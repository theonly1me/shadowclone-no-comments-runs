from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 7
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 1)) is False

    invoice = BillingService(store).generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 21)
    ]
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscriptions_raise_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 10))


def test_usage_boundaries_and_late_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 1, date(2025, 12, 31))
    billing.record_usage("s1", "start", 2, date(2026, 1, 1))
    billing.record_usage("s1", "last", 3, date(2026, 1, 30))
    billing.record_usage("s1", "end", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 5, date(2026, 3, 2))

    assert billing.generate_invoice("s1").total_cents == 3060
    billing.record_usage("s1", "arrived_late", 6, date(2026, 1, 10))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_is_scoped_to_subscription(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 2)) is True
    assert billing.generate_invoice("s1").total_cents == 3010
    assert billing.generate_invoice("s2").total_cents == 3020


def test_multiple_plan_changes_and_segment_allowances(billing, store):
    store.add_plan(Plan("basic", "Basic", 3001, included_units=10, overage_unit_price_cents=5))
    store.add_plan(Plan("pro", "Pro", 6001, included_units=20, overage_unit_price_cents=9))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "first", 3, date(2026, 1, 10))
    billing.record_usage("s1", "second", 8, date(2026, 1, 11))
    billing.record_usage("s1", "third", 4, date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 18),
        ("overage", "Overage: Basic", 5),
    ]
    assert invoice.total_cents == 4033
    assert store.get_subscription("s1").plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 3001


def test_half_up_proration_and_final_plan_carries_forward(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1, 2]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert billing.generate_invoice("s1").line_items[-1].description == "Plan: Basic"


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("missing", date(2026, 1, 10), KeyError),
])
def test_rejected_change_leaves_state_unchanged(billing, store, new_plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 9)),
    ("pro", date(2026, 1, 20)),
])
def test_changes_must_follow_previous_change(billing, store, new_plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before


def test_usage_subtotal_discount_credit_tax_and_storage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 1))
    billing.record_usage("s1", "second", 3, date(2026, 1, 16))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500

    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000),
        ("overage", 200), ("overage", 600),
        ("discount", -530), ("credit", -500), ("tax", 427),
    ]
    assert invoice.total_cents == 4697
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) is invoice


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 4520


def test_overage_line_exists_even_when_unit_price_is_zero(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0)
    ]


def test_included_usage_has_no_overage_line(billing, store):
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]


def test_credit_is_capped_after_discount_on_usage_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 5, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 500), ("discount", -500), ("credit", -3000)
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000
