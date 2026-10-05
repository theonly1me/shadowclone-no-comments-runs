from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [
        (item.kind, item.description, item.amount_cents)
        for item in invoice.line_items
    ]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_for_invalid_retry(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2030, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
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
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 1))
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 1))


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
    billing.record_usage("s1", "new-late", 6, date(2026, 1, 15))
    # Usage and plan history must survive recreation of the service.
    recreated = BillingService(store)
    assert recreated.generate_invoice("s1").total_cents == 3100
    assert recreated.generate_invoice("s1").total_cents == 3050
    assert recreated.generate_invoice("s1").total_cents == 3000


def test_included_usage_and_zero_price_overage_line(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    billing.record_usage("s1", "included", 10, date(2026, 1, 2))
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]
    plan.overage_unit_price_cents = 0
    billing.record_usage("s1", "extra", 11, date(2026, 2, 2))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_multiple_changes_segment_allowances_and_usage_attribution(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 5
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=8,
                        overage_unit_price_cents=20))
    store.add_plan(Plan("max", "Max", 9000, included_units=11,
                        overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 1)),
        ("basic", 3, date(2026, 1, 10)),
        ("pro", 5, date(2026, 1, 11)),
        ("max", 7, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 40),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Max", 120),
    ]
    assert invoice.total_cents == 6220
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert (invoice.period_start, invoice.period_end) == (
        date(2026, 1, 1), date(2026, 1, 31)
    )
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Max", 9000)]
    billing.change_plan("s1", "basic", date(2026, 3, 3))


def test_plan_proration_rounds_each_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 3001
    store.add_plan(Plan("pro", "Pro", 4001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1501), ("plan", "Plan: Pro", 2001),
    ]


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("pro", date(2026, 1, 1)),
    ("pro", date(2025, 12, 31)),
    ("pro", date(2026, 1, 31)),
    ("pro", date(2026, 2, 1)),
    ("basic", date(2026, 1, 10)),
])
def test_rejected_initial_change_leaves_state_unchanged(
    billing, store, new_plan_id, effective_on
):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 9)),
    ("basic", date(2026, 1, 10)),
    ("pro", date(2026, 1, 11)),
])
def test_rejected_later_change_preserves_previous_change(
    billing, store, new_plan_id, effective_on
):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_usage_subtotal_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "usage", 10, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=100))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "usage", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4600
