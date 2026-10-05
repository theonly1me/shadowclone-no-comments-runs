from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def plans(store):
    basic = store.get_plan("basic")
    basic.included_units = 100
    basic.overage_unit_price_cents = 5
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=3,
        )
    )
    store.add_plan(Plan(plan_id="lite", name="Lite", monthly_price_cents=15))
    return store


def kinds_and_amounts(invoice):
    return [(item.kind, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_then_false_for_duplicate(billing, plans):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_when_billing(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 9999, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 3000), ("overage", 250)]


def test_event_ids_are_scoped_per_subscription(billing, plans, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, plans, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_adds_no_overage(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 3000)]


def test_event_on_period_end_waits_for_next_invoice(billing, plans):
    billing.record_usage("s1", "e1", 130, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds_and_amounts(first) == [("plan", 3000)]
    assert kinds_and_amounts(second) == [("plan", 3000), ("overage", 150)]


def test_event_is_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 130, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds_and_amounts(first) == [("plan", 3000), ("overage", 150)]
    assert kinds_and_amounts(second) == [("plan", 3000)]


def test_late_event_is_billed_on_open_period(billing, plans):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 50, date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 2, 10))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert kinds_and_amounts(invoice) == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", 85),
    ]


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, plans, store):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 250, date(2026, 1, 20))
    billing.record_usage("s1", "e3", 1, date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", 85),
        ("overage", 153),
    ]
    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Overage: Basic",
        "Overage: Pro",
    ]
    assert invoice.total_cents == 5238
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_next_period_bills_new_plan_in_full(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 6000)]


def test_several_changes_in_one_period(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [
        ("plan", 1000),
        ("plan", 2000),
        ("plan", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_proration_rounds_half_up(billing, plans):
    billing.change_plan("s1", "lite", date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 2900), ("plan", 1)]


def test_included_units_are_floored_per_segment(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 3))
    billing.record_usage("s1", "e1", 7, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert ("overage", 5) in kinds_and_amounts(invoice)


def test_discount_credit_and_tax_apply_after_overage(billing, plans, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert kinds_and_amounts(invoice) == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, plans, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_not_after_previous_change_is_rejected(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = list(store.get_subscription("s1").plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))

    assert store.get_subscription("s1").plan_changes == before


def test_change_to_current_plan_is_rejected(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 15))


def test_change_to_unknown_plan_raises_key_error(billing, plans, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_change_for_unknown_subscription_raises_key_error(billing, plans):
    with pytest.raises(KeyError):
        billing.change_plan("missing", "pro", date(2026, 1, 11))
