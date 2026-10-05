from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def charges(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def add_usage_plans(store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=30, overage_unit_price_cents=10))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=20))


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == plan.overage_unit_price_cents == 0


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_record_event(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2))


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_usage_idempotency_survives_invoicing_and_new_service(billing, store):
    add_usage_plans(store)
    assert billing.record_usage("s1", "event", 40, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 2)) is False
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 100),
    ]
    other_service = BillingService(store)
    assert other_service.record_usage("s1", "event", 100, date(2026, 2, 1)) is False
    assert charges(other_service.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2))
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 2))


def test_future_events_wait_and_late_events_bill_in_open_period(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "last-day", 3, date(2026, 1, 30))
    billing.record_usage("s1", "boundary", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 5, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3050
    billing.record_usage("s1", "new-late", 6, date(2026, 1, 3))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_usage_boundaries_and_next_period(billing, store):
    add_usage_plans(store)
    store.add_plan(Plan("max", "Max", 9000, included_units=90, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))
    billing.record_usage("s1", "basic", 7, date(2026, 1, 10))
    billing.record_usage("s1", "pro", 23, date(2026, 1, 11))
    billing.record_usage("s1", "max", 34, date(2026, 1, 21))
    invoice = BillingService(store).generate_invoice("s1")
    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Max", 120),
    ]
    assert invoice.total_cents == 6200
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Max", 9000)]


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)])
def test_changes_must_be_strictly_inside_period(billing, store, effective_on):
    add_usage_plans(store)
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("plan_id,effective_on", [
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 16)),
    ("pro", date(2026, 1, 20)),
])
def test_rejected_changes_preserve_pending_state(billing, store, plan_id, effective_on):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_same_initial_plan_is_rejected_and_return_to_old_plan_is_allowed(billing, store):
    add_usage_plans(store)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000
    assert store.get_subscription("s1").plan_id == "basic"


def test_proration_rounds_half_up_and_included_units_round_down(billing, store):
    subscription = store.get_subscription("s1")
    subscription.period_end = date(2026, 1, 3)
    store.add_plan(Plan("basic", "Basic", 1, included_units=3, overage_unit_price_cents=5))
    store.add_plan(Plan("pro", "Pro", 3, included_units=3, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    billing.record_usage("s1", "a", 2, date(2026, 1, 1))
    billing.record_usage("s1", "b", 2, date(2026, 1, 2))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2),
        ("overage", "Overage: Basic", 5), ("overage", "Overage: Pro", 10),
    ]


def test_usage_subtotal_gets_discount_credit_then_tax(billing, store):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 25, date(2026, 1, 1))
    billing.record_usage("s1", "b", 40, date(2026, 1, 16))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000), ("overage", 100), ("overage", 200),
        ("discount", -480), ("credit", -500), ("tax", 382),
    ]
    assert invoice.total_cents == 4202
    assert customer.credit_balance_cents == 0


def test_credit_is_capped_at_usage_subtotal_after_discount(billing, store):
    add_usage_plans(store)
    billing.record_usage("s1", "a", 40, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=100))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 100), ("discount", -100), ("credit", -3000),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 50, date(2026, 1, 1))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4850


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "a", 1, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]
