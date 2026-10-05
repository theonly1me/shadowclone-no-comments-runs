from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def metered(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=100,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
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


def test_record_usage_is_idempotent(billing, metered):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 500),
    ]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", -3, date(2026, 1, 5))


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_adds_no_overage(billing, metered):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_usage_on_or_after_period_end_waits_for_next_invoice(billing, metered):
    billing.record_usage("s1", "e1", 110, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 100) in lines(second)
    assert [item.kind for item in third.line_items] == ["plan"]


def test_late_usage_is_billed_on_open_period(billing, metered):
    billing.generate_invoice("s1")
    assert billing.record_usage("s1", "late", 120, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "late", 120, date(2026, 1, 10)) is False

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 200) in lines(invoice)


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 50, date(2026, 1, 1))
    billing.record_usage("s1", "b", 10, date(2026, 1, 10))
    billing.record_usage("s1", "c", 250, date(2026, 1, 11))
    billing.record_usage("s1", "d", 10, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 270),
        ("overage", "Overage: Pro", 300),
    ]
    assert invoice.total_cents == 5570


def test_late_usage_goes_to_first_segment(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 15))
    billing.change_plan("s1", "pro", date(2026, 2, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 70) in lines(invoice)


def test_multiple_changes_in_one_period(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.change_plan("s1", "basic", date(2026, 1, 15))
    billing.change_plan("s1", "metered", date(2026, 1, 22))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 700),
        ("plan", "Plan: Pro", 1400),
        ("plan", "Plan: Basic", 700),
        ("plan", "Plan: Metered", 900),
    ]


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    store.add_plan(Plan(plan_id="free", name="Free", monthly_price_cents=0))
    billing.change_plan("s1", "free", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert invoice.line_items[0].amount_cents == 501


def test_change_plan_updates_subscription_after_invoice(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 20))

    billing.generate_invoice("s1")
    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_plan_rejects_dates_outside_period(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert metered.get_subscription("s1").plan_changes == []


def test_change_plan_must_be_after_previous_change(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))

    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_change_plan_to_current_plan_is_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_plan_unknown_plan_or_subscription(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert metered.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

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
