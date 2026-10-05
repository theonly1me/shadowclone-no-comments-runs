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
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_then_false_for_duplicate(billing, metered):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -3])
def test_record_usage_rejects_non_positive_units(billing, metered, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_adds_no_overage(billing, metered):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]
    assert invoice.total_cents == 3000


def test_future_event_waits_and_events_bill_once(billing, metered):
    billing.record_usage("s1", "now", 110, date(2026, 1, 10))
    billing.record_usage("s1", "later", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert first.total_cents == 3000 + 10 * 5
    assert second.total_cents == 3000 + 20 * 5
    assert third.total_cents == 3000


def test_late_event_is_billed_on_open_period(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 101, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 5)


def test_plan_change_prorates_and_splits_usage(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 5135


def test_late_event_goes_to_first_segment(billing, metered):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items if item.kind == "overage"] == [
        "Overage: Metered"
    ]


def test_multiple_changes_use_half_up_rounding(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.add_plan(Plan(plan_id="even", name="Even", monthly_price_cents=2000))
    billing.change_plan("s1", "odd", date(2026, 1, 16))
    billing.change_plan("s1", "basic", date(2026, 1, 22))
    billing.change_plan("s1", "even", date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Odd", 200),
        ("plan", "Plan: Basic", 300),
        ("plan", "Plan: Even", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "even"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="cheap", name="Cheap", monthly_price_cents=1))
    store.get_subscription("s1").plan_id = "cheap"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Cheap", 1)


def test_included_units_floor_per_segment(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.record_usage("s1", "a", 24, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2] == ("overage", "Overage: Metered", 5)


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_rejected(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert metered.get_subscription("s1").plan_changes == []


def test_change_must_be_after_previous_change(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))

    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_rejected(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    assert metered.get_subscription("s1").plan_changes == []


def test_discount_credit_tax_apply_to_usage_subtotal(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))

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
