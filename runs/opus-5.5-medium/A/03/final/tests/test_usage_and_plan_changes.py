from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def metered_plans(store):
    # The fixture period is 2026-01-01 to 2026-01-31: 30 days.
    basic = store.get_plan("basic")
    basic.included_units = 100
    basic.overage_unit_price_cents = 5
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=1000,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="lite", name="Lite", monthly_price_cents=1000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


# record_usage


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_completely(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 10_000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 250) in lines(invoice)


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_raise(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


# Usage billing


def test_usage_within_allowance_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Basic", 3000)]


def test_overage_is_billed_per_unit(billing):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 60, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]
    assert invoice.total_cents == 3100


def test_event_on_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert ("overage", "Overage: Basic", 250) in lines(second)


def test_events_are_billed_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # open period is now 2026-01-31 to 2026-03-02
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 150) in lines(invoice)


# change_plan


def test_mid_period_upgrade_prorates_and_splits_usage(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 days pro
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # basic includes 33
    billing.record_usage("s1", "b", 700, date(2026, 1, 11))  # pro includes 666

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 35),
        ("overage", "Overage: Pro", 68),
    ]
    assert invoice.total_cents == 5103


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")  # open period 2026-01-31 to 2026-03-02, 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    # Basic segment is 10 days, so 33 included; 17 over at 5 cents.
    assert ("overage", "Overage: Basic", 85) in lines(invoice)
    assert not any(d == "Overage: Pro" for _, d, _ in lines(invoice))


def test_multiple_changes_and_rounding(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 8))  # 7 days basic
    billing.change_plan("s1", "lite", date(2026, 1, 21))  # 13 days pro, 10 days lite

    invoice = billing.generate_invoice("s1")

    # 3000*7/30 = 700; 6000*13/30 = 2600; 1000*10/30 = 333.33
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 700),
        ("plan", "Plan: Pro", 2600),
        ("plan", "Plan: Lite", 333),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "lite"
    assert subscription.plan_changes == []


def test_proration_rounds_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 15
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    billing.change_plan("s1", "odd", date(2026, 1, 2))  # 1 day and 29 days

    invoice = billing.generate_invoice("s1")

    # 15/30 = 0.5 -> 1; 45*29/30 = 43.5 -> 44
    assert [i.amount_cents for i in invoice.line_items] == [1, 44]


def test_next_period_uses_new_plan_in_full(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31)]
)
def test_change_outside_open_period_raises(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_not_after_previous_change_raises(billing, store, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "lite", effective_on)
    assert store.get_subscription("s1").plan_changes == [(date(2026, 1, 10), "pro")]


def test_change_to_current_plan_raises(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    # Switching back to the original plan is allowed.
    billing.change_plan("s1", "basic", date(2026, 1, 20))


def test_unknown_plan_or_subscription_raises_key_error(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_rejected_change_leaves_invoice_unchanged(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Basic", 3000)]


# Adjustments on top of usage and proration


def test_discount_credit_and_tax_apply_to_full_subtotal(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 16))  # 15 days each
    billing.record_usage("s1", "e1", 70, date(2026, 1, 3))  # basic includes 50

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1500 + 3000 + 100 = 4600; discount 460; credit 500; tax 364
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 3000),
        ("overage", 100),
        ("discount", -460),
        ("credit", -500),
        ("tax", 364),
    ]
    assert invoice.total_cents == 4004
    assert customer.credit_balance_cents == 0
