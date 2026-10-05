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


def test_usage_is_idempotent_before_validation_and_across_periods(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2030, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    recreated = BillingService(store)
    assert recreated.record_usage("s1", "event", 50, date(2026, 2, 1)) is False
    assert recreated.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", units, date(2026, 1, 1))
    assert subscription == before
    assert billing.record_usage("s1", "bad", 1, date(2026, 1, 1)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 15))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 1)) is True


def test_usage_boundaries_future_and_late_events(billing, store):
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
    billing.record_usage("s1", "new-late", 6, date(2026, 1, 10))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_aggregates_before_applying_allowance(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 20
    billing.record_usage("s1", "one", 4, date(2026, 1, 2))
    billing.record_usage("s1", "two", 4, date(2026, 1, 3))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 60),
    ]


def test_multiple_changes_segment_allowances_and_line_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 1)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 9, date(2026, 1, 11)),
        ("third", 7, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Basic", 40),
    ]
    assert invoice.total_cents == 4120
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_subscription("s1").plan_changes == []


def test_plan_rounding_is_half_up_per_segment(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2),
    ]


def test_final_plan_carries_forward_and_new_period_accepts_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.generate_invoice("s1")
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert subscription.plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 6000
    billing.change_plan("s1", "basic", date(2026, 3, 3))


@pytest.mark.parametrize("new_plan_id, effective_on, error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 15), ValueError),
    ("missing", date(2026, 1, 15), KeyError),
])
def test_rejected_initial_change_is_atomic(billing, store, new_plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("new_plan_id, effective_on", [
    ("basic", date(2026, 1, 14)),
    ("basic", date(2026, 1, 15)),
    ("pro", date(2026, 1, 16)),
])
def test_rejected_subsequent_change_is_atomic(billing, store, new_plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_discount_credit_tax_apply_to_plan_and_overage_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "one", 1, date(2026, 1, 1))
    billing.record_usage("s1", "two", 2, date(2026, 1, 16))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000),
        ("overage", 100), ("overage", 400),
        ("discount", -500), ("credit", -500), ("tax", 400),
    ]
    assert invoice.total_cents == 4400
    assert customer.credit_balance_cents == 0


def test_discount_and_credit_caps_with_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("BIG", amount_off_cents=10000))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 10000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "one", 2, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "BIG")
    assert [item.kind for item in invoice.line_items] == ["plan", "overage", "discount"]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 10000
    billing.record_usage("s1", "two", 3, date(2026, 2, 1))
    assert billing.generate_invoice("s1").total_cents == 0
    assert customer.credit_balance_cents == 6700


def test_zero_price_overage_line_is_kept(billing):
    billing.record_usage("s1", "one", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_failed_invoice_does_not_consume_events_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "one", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4510
