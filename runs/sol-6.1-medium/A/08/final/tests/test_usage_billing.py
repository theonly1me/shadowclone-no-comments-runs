from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_invoices_and_service_instances(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    assert billing.record_usage("s1", "event", 15, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 5, 1)) is False
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 35)
    ]
    other_service = BillingService(store)
    assert other_service.record_usage("s1", "event", 99, date(2026, 2, 1)) is False
    assert [item.kind for item in other_service.generate_invoice("s1").line_items] == ["plan"]


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "invalid", units, date(2026, 1, 1))
    assert store.get_subscription("s1") == before


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 1))
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 1))


def test_late_and_future_events_are_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "end", 3, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3020
    # A newly arriving event for the closed period goes on the next invoice.
    billing.record_usage("s1", "new-late", 5, date(2026, 1, 1))
    assert billing.generate_invoice("s1").total_cents == 3080
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_usage_boundaries_and_end_plan(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 2
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=3))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("start", 3, date(2026, 1, 1)),
        ("before", 1, date(2026, 1, 10)),
        ("change", 8, date(2026, 1, 11)),
        ("last", 5, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 6),
        ("overage", "Overage: Pro", 6),
        ("overage", "Overage: Basic", 4),
    ]
    assert invoice.total_cents == 4016
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert (subscription.period_start, subscription.period_end) == (date(2026, 1, 31), date(2026, 3, 2))
    # Changes in a fresh period are validated against that period only.
    billing.change_plan("s1", "pro", date(2026, 2, 1))
    billing.generate_invoice("s1")
    assert subscription.plan_id == "pro"
    assert billing.generate_invoice("s1").total_cents == 6000


@pytest.mark.parametrize("plan_id,effective_on", [
    ("basic", date(2026, 1, 12)),
    ("pro", date(2026, 1, 1)),
    ("pro", date(2026, 1, 31)),
    ("pro", date(2025, 12, 31)),
    ("pro", date(2026, 2, 1)),
])
def test_rejected_initial_change_is_atomic(billing, store, plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("plan_id,effective_on", [
    ("pro", date(2026, 1, 20)),
    ("basic", date(2026, 1, 11)),
    ("basic", date(2026, 1, 10)),
])
def test_rejected_followup_change_is_atomic(billing, store, plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_each_plan_segment_rounds_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2)
    ]


def test_usage_subtotal_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) is invoice


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1"))[-1] == ("overage", "Overage: Basic", 0)
