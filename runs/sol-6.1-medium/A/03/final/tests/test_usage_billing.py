from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_per_subscription_even_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 5, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.record_usage("s1", "event", 999, date(2026, 2, 1)) is False
    # State belongs to the store, not to a particular service instance.
    assert BillingService(store).generate_invoice("s1").total_cents == 3000

    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 1)) is True
    assert billing.generate_invoice("s2").total_cents == 3020


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_usage_boundaries_late_events_and_future_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    for event_id, units, occurred_on in [
        ("late", 1, date(2025, 12, 31)),
        ("start", 2, date(2026, 1, 1)),
        ("last", 3, date(2026, 1, 30)),
        ("end", 4, date(2026, 1, 31)),
        ("future", 5, date(2026, 3, 2)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    assert billing.generate_invoice("s1").total_cents == 3060
    billing.record_usage("s1", "arrived-late", 6, date(2026, 1, 10))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_proration_usage_and_next_period(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=11))
    store.add_plan(Plan("max", "Max", 9000, included_units=30, overage_unit_price_cents=13))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("basic", 3, date(2026, 1, 10)),
        ("pro", 9, date(2026, 1, 11)),
        ("max", 14, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 14),
        ("overage", "Overage: Pro", 33),
        ("overage", "Overage: Max", 52),
    ]
    assert invoice.total_cents == 6099
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) is invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Max", 9000)]


def test_each_plan_segment_rounds_half_up_independently(billing, store):
    store.get_plan("basic").monthly_price_cents = 1001
    store.add_plan(Plan("pro", "Pro", 2001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 501),
        ("plan", "Plan: Pro", 1001),
    ]


def test_allowance_is_per_segment_not_shared(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=2, overage_unit_price_cents=15))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "event", 2, date(2026, 1, 11))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Pro", 15),
    ]


def test_usage_with_zero_overage_price_still_has_overage_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


@pytest.mark.parametrize("units,overage", [(9, 0), (10, 0), (11, 7)])
def test_full_period_included_units(billing, store, units, overage):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "event", units, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1")
    assert invoice.total_cents == 3000 + overage
    assert [item.kind for item in invoice.line_items] == (
        ["plan", "overage"] if overage else ["plan"]
    )


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 2), ValueError),
    ("missing", date(2026, 1, 2), KeyError),
])
def test_rejected_first_change_leaves_state_unchanged(billing, store, new_plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 11)),
    ("pro", date(2026, 1, 12)),
])
def test_rejected_later_change_leaves_history_unchanged(billing, store, new_plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before
    # Returning to a previous plan is valid if the date is strictly later.
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000
    # The prior period's changes no longer constrain the next period.
    billing.change_plan("s1", "pro", date(2026, 2, 1))


@pytest.mark.parametrize("credit,expected,balance", [
    (500, [("plan", 3000), ("overage", 1000), ("discount", -400), ("credit", -500), ("tax", 310)], 0),
    (5000, [("plan", 3000), ("overage", 1000), ("discount", -400), ("credit", -3600)], 1400),
])
def test_overage_is_in_discount_credit_and_tax_base(billing, store, credit, expected, balance):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 20, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = credit
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == expected
    assert invoice.total_cents == sum(amount for _, amount in expected)
    assert customer.credit_balance_cents == balance


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 10, date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 4600
