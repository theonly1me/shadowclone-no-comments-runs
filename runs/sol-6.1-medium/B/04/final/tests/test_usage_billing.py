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


def test_usage_idempotency_is_per_subscription_and_survives_billing(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "event", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 1)) is False
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 2)) is True
    billing.generate_invoice("s1")
    assert BillingService(store).record_usage("s1", "event", -1, None) is False


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_usage_allowance_and_once_only_billing(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "a", 6, date(2026, 1, 1))
    billing.record_usage("s1", "b", 9, date(2026, 1, 30))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 35)
    ]
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_future_events_wait_and_late_events_bill_in_current_period(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    billing.record_usage("s1", "old", 4, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3040
    billing.record_usage("s1", "late", 5, date(2026, 1, 2))
    assert billing.generate_invoice("s1").total_cents == 3070
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_attribute_boundaries_and_late_events(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000, included_units=6, overage_unit_price_cents=20))
    store.add_plan(Plan("pro", "Pro", 9000, included_units=9, overage_unit_price_cents=30))
    basic = store.get_plan("basic")
    basic.included_units = 3
    basic.overage_unit_price_cents = 10
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("first", 1, date(2026, 1, 10)),
        ("second", 5, date(2026, 1, 11)),
        ("third", 7, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = BillingService(store).generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Plus", 2000),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Plus", 60),
        ("overage", "Overage: Pro", 120),
    ]
    assert invoice.total_cents == 6200
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert billing.generate_invoice("s1").total_cents == 3200


@pytest.mark.parametrize("effective_on", [
    date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)
])
def test_change_must_be_strictly_inside_period(billing, store, effective_on):
    store.add_plan(Plan("plus", "Plus", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("plus", date(2026, 1, 20)),
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 9)),
])
def test_rejected_change_preserves_all_state(billing, store, new_plan_id, effective_on):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_proration_rounds_half_up_and_allowances_round_down(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.get_plan("basic").included_units = 3
    store.get_plan("basic").overage_unit_price_cents = 5
    store.add_plan(Plan("plus", "Plus", 3, included_units=3, overage_unit_price_cents=7))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 15))
    billing.record_usage("s1", "second", 2, date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Plus", 2),
        ("overage", "Overage: Basic", 5), ("overage", "Overage: Plus", 7),
    ]


def test_discount_credit_and_tax_include_usage_and_proration(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("plus", "Plus", 6000, overage_unit_price_cents=200))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 1))
    billing.record_usage("s1", "second", 4, date(2026, 1, 16))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 1000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000), ("overage", 200), ("overage", 800),
        ("discount", -550), ("credit", -1000), ("tax", 395),
    ]
    assert invoice.total_cents == 4345
    assert customer.credit_balance_cents == 0


def test_zero_price_overage_line_is_kept(billing):
    billing.record_usage("s1", "free", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_unused_allowance_does_not_transfer_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("plus", "Plus", 6000, included_units=2, overage_unit_price_cents=10))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "event", 4, date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500), ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Plus", 30),
    ]


def test_changing_back_to_original_plan_keeps_separate_segments(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1000), ("plan", "Plan: Plus", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_credit_is_capped_after_usage_discount(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 5, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 4000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "FIXED")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 500), ("discount", -500), ("credit", -3000),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1000


def test_failed_invoice_does_not_consume_usage_or_plan_changes(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "event", 5, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4550
