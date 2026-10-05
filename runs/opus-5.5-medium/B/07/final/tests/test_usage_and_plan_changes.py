from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture
def metered(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=90,
            overage_unit_price_cents=5,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_then_false_for_duplicates(billing, metered):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    from ledger.models import Subscription

    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_included_units_has_no_overage(billing, metered):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_events_wait_and_events_bill_once(billing, metered):
    billing.record_usage("s1", "now", 31, date(2026, 1, 30))
    billing.record_usage("s1", "later", 35, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 10) in lines(first)
    assert ("overage", "Overage: Metered", 50) in lines(second)
    assert [item.kind for item in third.line_items] == ["plan"]


def test_late_event_billed_on_open_period(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 100) in lines(invoice)


def test_mid_cycle_change_prorates_and_splits_usage(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 25, date(2026, 1, 10))
    billing.record_usage("s1", "b", 70, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 150),
        ("overage", "Overage: Pro", 50),
    ]
    assert invoice.total_cents == 5200
    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_late_events_go_to_first_segment(billing, metered):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 1))
    billing.record_usage("s1", "late", 10, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 100),
        ("plan", "Plan: Pro", 5800),
        ("overage", "Overage: Metered", 90),
    ]


def test_multiple_changes_with_rounding(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.change_plan("s1", "basic", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 700),
        ("plan", "Plan: Pro", 2400),
        ("plan", "Plan: Basic", 1100),
    ]
    assert metered.get_subscription("s1").plan_id == "basic"


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [501, 1500]


def test_included_units_floor_per_segment(billing, metered):
    metered.add_plan(
        Plan(
            plan_id="small",
            name="Small",
            monthly_price_cents=0,
            included_units=10,
            overage_unit_price_cents=7,
        )
    )
    metered.get_subscription("s1").plan_id = "small"
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.record_usage("s1", "a", 3, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Small", 7) in lines(invoice)


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_period_is_rejected(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert metered.get_subscription("s1").plan_changes == []


def test_change_must_be_after_previous_change(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    billing.change_plan("s1", "metered", date(2026, 1, 20))


def test_unknown_plan_or_subscription_raises_key_error(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    subscription = metered.get_subscription("s1")
    assert subscription.plan_changes == []
    assert subscription.plan_id == "metered"


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 130, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
