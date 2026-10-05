from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_with_invalid_replacement(billing, store):
    plan = store.get_plan("basic")
    plan.overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert subscription == before
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 15))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "shared", 2, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "shared", 3, date(2026, 1, 1)) is True


def test_future_and_late_events_billed_once_across_service_instances(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "first", 1, date(2026, 1, 30))
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010

    billing = BillingService(store)
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_included_usage_is_aggregated(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 5
    billing.record_usage("s1", "one", 7, date(2026, 1, 1))
    billing.record_usage("s1", "two", 6, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 15),
    ]


def test_multiple_segments_rounding_allowances_and_event_boundaries(billing, store):
    store.add_plan(Plan("basic", "Basic", 3001, included_units=31,
                        overage_unit_price_cents=5))
    store.add_plan(Plan("pro", "Pro", 6002, included_units=32,
                        overage_unit_price_cents=7))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 11, date(2025, 12, 1))
    billing.record_usage("s1", "first", 1, date(2026, 1, 10))
    billing.record_usage("s1", "second", 12, date(2026, 1, 11))
    billing.record_usage("s1", "third", 14, date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents)
            for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2001),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 14),
        ("overage", "Overage: Basic", 20),
    ]
    assert invoice.total_cents == 4045
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) is invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_changes == []
    assert subscription.plan_id == "basic"
    assert billing.generate_invoice("s1").total_cents == 3001


def test_proration_uses_half_up(billing, store):
    subscription = store.get_subscription("s1")
    subscription.period_end = date(2026, 1, 3)
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 1))
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1, 1]
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 3)
    assert subscription.period_end == date(2026, 1, 5)
    # Ordering is scoped to the open period, not historical changes.
    billing.change_plan("s1", "basic", date(2026, 1, 4))


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("missing", date(2026, 1, 20), KeyError),
    ("pro", date(2026, 1, 20), ValueError),
    ("basic", date(2026, 1, 1), ValueError),
    ("basic", date(2026, 1, 31), ValueError),
    ("basic", date(2025, 12, 31), ValueError),
    ("basic", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("basic", date(2026, 1, 15), ValueError),
])
def test_rejected_changes_are_atomic(billing, store, new_plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before


def test_cannot_change_to_initial_current_plan(billing, store):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 15))
    assert store.get_subscription("s1") == before


def test_usage_subtotal_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 100), ("discount", -310),
        ("credit", -500), ("tax", 229),
    ]
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0


def test_credit_is_capped_after_usage_discount(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=100))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 4000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 100), ("discount", -100), ("credit", -3000),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1000


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    billing.record_usage("s1", "event", 2, date(2026, 1, 15))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert subscription == before
    assert billing.generate_invoice("s1").line_items[-1].amount_cents == 20


def test_positive_overage_with_zero_price_still_has_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0),
    ]
