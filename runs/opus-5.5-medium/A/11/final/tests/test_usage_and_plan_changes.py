from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription

# The fixture period is [2026-01-01, 2026-01-31): 30 days.


@pytest.fixture
def plans(store):
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


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# record_usage


def test_record_usage_returns_true_then_false_for_duplicate(billing, plans):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_completely(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 50 * 5)


def test_event_ids_are_scoped_per_subscription(billing, store, plans):
    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_raise(billing, plans, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected event did not consume the id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


# Usage billing


def test_usage_within_allowance_adds_no_overage(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_over_allowance_bills_overage(billing, plans):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 60, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 20 * 5),
    ]
    assert invoice.total_cents == 3100


def test_event_on_period_end_waits_for_next_invoice(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Metered", 50 * 5)


def test_events_are_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing, plans):
    billing.generate_invoice("s1")  # open period is now [01-31, 03-02)
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 30 * 5)


# change_plan


def test_mid_period_upgrade_prorates_and_splits_usage(billing, store, plans):
    # Metered for 10 days, Pro for 20 days.
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))  # Metered, 33 included
    billing.record_usage("s1", "e2", 250, date(2026, 1, 11))  # Pro, 200 included

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 7 * 5),
        ("overage", "Overage: Pro", 50 * 2),
    ]
    assert invoice.total_cents == 5135
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_proration_rounds_half_up(billing, store, plans):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1005))
    store.get_subscription("s1").plan_id = "odd"
    # Odd for 3 days, Metered for 1 day, Odd for 26 days.
    billing.change_plan("s1", "metered", date(2026, 1, 4))
    billing.change_plan("s1", "odd", date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Odd", 101),  # 1005 * 3 / 30 = 100.5
        ("plan", "Plan: Metered", 100),
        ("plan", "Plan: Odd", 871),  # 1005 * 26 / 30 = 871.0
    ]
    assert store.get_subscription("s1").plan_id == "odd"


def test_late_event_goes_to_first_segment(billing, plans):
    billing.generate_invoice("s1")  # open period is [01-31, 03-02), 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 15))  # 15 days each
    billing.record_usage("s1", "late", 60, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    # Metered segment includes 100 * 15 // 30 = 50 units.
    assert ("overage", "Overage: Metered", 10 * 5) in lines(invoice)
    assert not any(line[1] == "Overage: Pro" for line in lines(invoice))


def test_multiple_changes_and_changes_are_cleared(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")
    assert [line[1] for line in lines(invoice)] == [
        "Plan: Metered",
        "Plan: Pro",
        "Plan: Metered",
    ]
    assert store.get_subscription("s1").plan_id == "metered"

    next_invoice = billing.generate_invoice("s1")
    assert lines(next_invoice) == [("plan", "Plan: Metered", 3000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_raises(billing, plans, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_not_after_previous_change_raises(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")
    assert [line[1] for line in lines(invoice)] == ["Plan: Metered", "Plan: Pro"]


def test_change_to_current_plan_raises(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription_raises(billing, store, plans):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store, plans):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 3000 + 1000 = 4000; discount 400; credit 500; tax on 3100.
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
