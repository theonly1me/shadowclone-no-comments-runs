from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_with_invalid_replacement(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 5
    assert billing.record_usage("s1", "event", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "event", 0, None) is False
    invoice = BillingService(store).generate_invoice("s1")
    assert invoice.total_cents == 3050
    assert billing.record_usage("s1", "event", 20, date(2026, 2, 5)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_no_event(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "event", 1, date(2026, 1, 1)) is True


def test_usage_boundaries_late_events_and_bill_once(billing, store):
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
    billing.record_usage("s1", "new-late", 6, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_included_usage_and_zero_price_overage(billing, store):
    store.get_plan("basic").included_units = 10
    billing.record_usage("s1", "included", 10, date(2026, 1, 1))
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]
    billing.record_usage("s1", "extra", 11, date(2026, 2, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


def test_multiple_changes_usage_attribution_and_period_reset(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=30, overage_unit_price_cents=10))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=20))
    store.add_plan(Plan("plus", "Plus", 9000, included_units=90, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "plus", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 3, date(2025, 12, 31)),
        ("first", 9, date(2026, 1, 10)),
        ("second", 23, date(2026, 1, 11)),
        ("third", 34, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = BillingService(store).generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Plus", 120),
    ]
    assert invoice.total_cents == 6200
    assert store.get_invoice(invoice.invoice_id) is invoice
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Plus", 300),
        ("plan", "Plan: Basic", 2900),
    ]


def test_segment_rounding_and_included_units_floor(billing, store):
    store.add_plan(Plan("basic", "Basic", 15, included_units=3, overage_unit_price_cents=7))
    store.add_plan(Plan("pro", "Pro", 15, included_units=3, overage_unit_price_cents=11))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 15))
    billing.record_usage("s1", "second", 2, date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 8),
        ("plan", "Plan: Pro", 8),
        ("overage", "Overage: Basic", 7),
        ("overage", "Overage: Pro", 11),
    ]


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("missing", date(2026, 1, 10), KeyError),
])
def test_rejected_change_preserves_state(billing, store, new_plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 9)),
    ("basic", date(2026, 1, 10)),
    ("pro", date(2026, 1, 11)),
])
def test_rejected_change_after_previous_change(billing, store, new_plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before


@pytest.mark.parametrize("credit,expected,remaining", [
    (500, [("discount", -460), ("credit", -500), ("tax", 364)], 0),
    (5000, [("discount", -460), ("credit", -4140)], 860),
])
def test_usage_subtotal_discount_credit_tax(billing, store, credit, expected, remaining):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=20))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = credit
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 4, date(2026, 1, 1))
    billing.record_usage("s1", "second", 3, date(2026, 1, 16))
    invoice = billing.generate_invoice("s1", "TEN")
    amounts = [(item.kind, item.amount_cents) for item in invoice.line_items]
    assert amounts == [
        ("plan", 1500), ("plan", 3000), ("overage", 40), ("overage", 60),
    ] + expected
    assert invoice.total_cents == sum(amount for _, amount in amounts)
    assert customer.credit_balance_cents == remaining


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 1, date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 4510
