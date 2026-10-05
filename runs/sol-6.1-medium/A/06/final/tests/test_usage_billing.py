from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def configure_plans(store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=30,
                        overage_unit_price_cents=2))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=60,
                        overage_unit_price_cents=3))


def test_plan_usage_defaults():
    plan = Plan("plain", "Plain", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_invoicing_and_service_instances(billing, store):
    configure_plans(store)
    assert billing.record_usage("s1", "event", 35, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", -1, date(2030, 1, 1)) is False
    invoice = BillingService(store).generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 10),
    ]
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic",
                                        date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2))
    assert billing.record_usage("s2", "event", 1, date(2026, 1, 2))


def test_future_and_late_events_are_billed_once(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, overage_unit_price_cents=2))
    billing.record_usage("s1", "late", 4, date(2025, 12, 31))
    billing.record_usage("s1", "boundary", 5, date(2026, 1, 31))
    billing.record_usage("s1", "future", 6, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3008
    billing.record_usage("s1", "new-late", 7, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3024
    assert billing.generate_invoice("s1").total_cents == 3012
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_attribute_usage_and_reset_period(billing, store):
    configure_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))
    billing.record_usage("s1", "first", 10, date(2026, 1, 1))
    billing.record_usage("s1", "second", 21, date(2026, 1, 11))
    billing.record_usage("s1", "third", 11, date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents)
            for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 3),
        ("overage", "Overage: Basic", 2),
    ]
    assert invoice.total_cents == 4015
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    billing.change_plan("s1", "pro", date(2026, 2, 1))
    billing.generate_invoice("s1")
    assert subscription.plan_id == "pro"
    assert billing.generate_invoice("s1").total_cents == 6000


@pytest.mark.parametrize("plan_id,effective_on", [
    ("basic", date(2026, 1, 1)),
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 11)),
    ("basic", date(2026, 1, 31)),
    ("basic", date(2026, 2, 1)),
    ("pro", date(2026, 1, 12)),
])
def test_rejected_plan_changes_leave_state_unchanged(billing, store, plan_id, effective_on):
    configure_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_reject_initial_same_plan(billing, store):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_proration_rounds_half_up_and_allowances_floor_per_segment(billing, store):
    store.add_plan(Plan("basic", "Basic", 1, included_units=1,
                        overage_unit_price_cents=2))
    store.add_plan(Plan("pro", "Pro", 1, included_units=1,
                        overage_unit_price_cents=3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 1, date(2026, 1, 15))
    billing.record_usage("s1", "second", 1, date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1, 1, 2, 3]


def test_usage_subtotal_receives_discount_credit_and_tax(billing, store):
    configure_plans(store)
    billing.record_usage("s1", "event", 80, date(2026, 1, 2))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 100), ("discount", -310),
        ("credit", -500), ("tax", 229),
    ]
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    configure_plans(store)
    billing.record_usage("s1", "event", 100, date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 5180
