from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def items(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_billing_and_service_recreation(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 7
    assert billing.record_usage("s1", "event", 4, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3028
    billing = BillingService(store)
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_is_not_recorded(billing, store, units):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", units, date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_usage_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2))
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 2))


def test_usage_boundaries_future_and_late_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    for event_id, units, occurred_on in [
        ("late", 1, date(2025, 12, 1)),
        ("start", 2, date(2026, 1, 1)),
        ("last", 3, date(2026, 1, 30)),
        ("end", 4, date(2026, 1, 31)),
        ("future", 5, date(2026, 3, 2)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    assert billing.generate_invoice("s1").total_cents == 3060
    billing.record_usage("s1", "arrived-late", 6, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_usage_attribution_and_next_period(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 9
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=12, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 1)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 7, date(2026, 1, 11)),
        ("third", 4, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = billing.generate_invoice("s1")
    assert items(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Basic", 10),
    ]
    assert invoice.total_cents == 4090
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert billing.generate_invoice("s1").total_cents == 3000


def test_proration_rounds_half_up_and_allowance_floors(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.get_plan("basic").included_units = 3
    store.get_plan("basic").overage_unit_price_cents = 5
    store.add_plan(Plan("pro", "Pro", 3, included_units=3, overage_unit_price_cents=7))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 2, date(2026, 1, 1))
    billing.record_usage("s1", "b", 2, date(2026, 1, 16))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1),
        ("plan", "Plan: Pro", 2),
        ("overage", "Overage: Basic", 5),
        ("overage", "Overage: Pro", 7),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert items(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 3)]


@pytest.mark.parametrize("plan_id,effective_on,error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("pro", date(2026, 1, 10), ValueError),
    ("pro", date(2026, 1, 9), ValueError),
    ("basic", date(2026, 1, 20), ValueError),
    ("missing", date(2026, 1, 20), KeyError),
])
def test_rejected_changes_are_atomic(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    store.add_plan(Plan("other", "Other", 9000))
    billing.change_plan("s1", "other", date(2026, 1, 5))
    billing.change_plan("s1", "basic", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_usage_subtotal_discount_credit_tax(billing, store):
    store.get_plan("basic").included_units = 2
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "a", 12, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_zero_price_overage_line_and_credit_cap(billing, store):
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=500))
    billing.record_usage("s1", "a", 1, date(2026, 1, 2))
    assert items(billing.generate_invoice("s1", "OFF")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
        ("discount", "Discount", -500), ("credit", "Account credit", -2500),
    ]
    assert customer.credit_balance_cents == 2500


def test_failed_invoice_does_not_consume_usage_or_plan_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 2, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4520
