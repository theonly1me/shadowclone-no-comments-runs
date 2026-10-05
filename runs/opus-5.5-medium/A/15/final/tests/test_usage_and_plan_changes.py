from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription

# The fixture subscription s1 bills [2026-01-01, 2026-01-31): 30 days.


@pytest.fixture(autouse=True)
def metered_plans(store):
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


def lines(invoice):
    return [
        (item.kind, item.description, item.amount_cents) for item in invoice.line_items
    ]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="x", name="X", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# Usage events


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 4)) is False


def test_duplicate_event_is_ignored_when_billing(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 3))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 4))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 500),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 3)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_are_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 3))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True


def test_usage_for_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 3))


def test_usage_within_allowance_adds_no_overage_line(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_on_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 10),
    ]


def test_usage_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan", "overage"]
    assert [item.kind for item in second.line_items] == ["plan"]


def test_resending_a_billed_event_is_still_a_duplicate(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 10))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 101, date(2026, 2, 10)) is False
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")  # Now open: [2026-01-31, 2026-03-02).
    billing.record_usage("s1", "late", 120, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 200),
    ]


# Plan changes


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered for 10 days: 33 units included. Pro for 20 days: 200 included.
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 170),
        ("overage", "Overage: Pro", 250),
    ]
    assert invoice.total_cents == 5420
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_plan_changes_are_cleared_after_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_several_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 40, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    # Late usage lands in the first (Metered) segment: 40 - 33 = 7 over.
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Metered", 70),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_changing_back_to_an_earlier_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
        "Plan: Metered",
    ]


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "metered", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # 1001 * 15 / 30 = 500.5
    assert lines(invoice)[0] == ("plan", "Plan: Odd", 501)


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_the_open_period_is_rejected(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


@pytest.mark.parametrize("effective_on", [date(2026, 1, 5), date(2026, 1, 11)])
def test_change_not_after_previous_change_is_rejected(billing, store, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", effective_on)

    invoice = billing.generate_invoice("s1")
    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
    ]


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    # Nothing was recorded.
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Metered", 3000)]


def test_change_for_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


# Discount, credit and tax on top of usage


def test_adjustments_apply_to_plan_and_overage_subtotal(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 10))

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
