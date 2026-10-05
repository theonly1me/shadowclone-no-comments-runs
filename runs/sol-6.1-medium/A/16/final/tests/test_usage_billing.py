from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def charges(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_billing_and_service_recreation(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 1)) is False
    invoice = BillingService(store).generate_invoice("s1")
    assert charges(invoice) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 30)
    ]
    assert BillingService(store).record_usage("s1", "event", -1, None) is False
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True


def test_unknown_subscriptions_and_plans(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_usage_period_boundaries_late_events_and_included_units(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "start", 3, date(2026, 1, 1))
    billing.record_usage("s1", "last", 4, date(2026, 1, 30))
    billing.record_usage("s1", "end", 6, date(2026, 1, 31))
    billing.record_usage("s1", "future", 10, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3014
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))
    assert billing.generate_invoice("s1").total_cents == 3021
    assert billing.generate_invoice("s1").total_cents == 3035
    assert billing.generate_invoice("s1").total_cents == 3000


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2))
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 2))


def test_multiple_changes_segment_usage_rounding_and_reset(billing, store):
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 1
    basic.included_units = 5
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 3, included_units=5, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.change_plan("s1", "basic", date(2026, 1, 26))
    billing.record_usage("s1", "late", 1, date(2025, 12, 1))
    billing.record_usage("s1", "first", 3, date(2026, 1, 15))
    billing.record_usage("s1", "second", 3, date(2026, 1, 16))
    billing.record_usage("s1", "third", 1, date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")
    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1),  # 0.5 rounds up
        ("plan", "Plan: Pro", 1),
        ("plan", "Plan: Basic", 0),
        ("overage", "Overage: Basic", 20),  # floor(5 * 15 / 30) = 2
        ("overage", "Overage: Pro", 40),  # floor(5 * 10 / 30) = 1
        ("overage", "Overage: Basic", 10),  # floor(5 * 5 / 30) = 0
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 1)]


@pytest.mark.parametrize("plan_id,effective_on", [
    ("pro", date(2026, 1, 1)),
    ("pro", date(2026, 1, 31)),
    ("pro", date(2025, 12, 31)),
    ("pro", date(2026, 2, 1)),
    ("pro", date(2026, 1, 20)),  # already current
    ("basic", date(2026, 1, 16)),  # same date as previous change
    ("basic", date(2026, 1, 15)),  # earlier than previous change
])
def test_rejected_changes_are_atomic(billing, store, plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_final_plan_carries_forward_and_new_period_accepts_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert billing.generate_invoice("s1").total_cents == 4500
    assert store.get_subscription("s1").plan_id == "pro"
    billing.change_plan("s1", "basic", date(2026, 2, 15))
    assert billing.generate_invoice("s1").total_cents == 4500


def test_overage_in_discount_credit_tax_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 1, date(2026, 1, 1))
    billing.record_usage("s1", "second", 2, date(2026, 1, 16))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000), ("overage", 100), ("overage", 400),
        ("discount", -500), ("credit", -500), ("tax", 400),
    ]
    assert invoice.total_cents == 4400
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4510


def test_positive_overage_has_line_even_with_zero_price(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]
