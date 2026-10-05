from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def amounts(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_billing_and_service_recreation(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, None) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    billing = BillingService(store)
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_ids(billing, store):
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
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert billing.record_usage("s2", "event", 1, date(2026, 1, 1))


def test_late_and_future_usage_is_billed_on_first_eligible_invoice(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "last-day", 3, date(2026, 1, 30))
    billing.record_usage("s1", "boundary", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 5, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3050
    billing.record_usage("s1", "new-late", 6, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_usage_and_adjustment_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 20
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=30))
    store.add_plan(Plan("plus", "Plus", 9000, included_units=30, overage_unit_price_cents=40))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "plus", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 8, date(2026, 1, 11)),
        ("third", 11, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert amounts(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 40),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Plus", 40),
        ("discount", "Discount", -614),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 503),
    ]
    assert invoice.total_cents == 5529
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert customer.credit_balance_cents == 0
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert amounts(billing.generate_invoice("s1"))[0] == ("plan", "Plan: Plus", 9000)


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)])
def test_change_must_be_inside_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


def test_rejected_changes_preserve_state_and_changing_back_is_allowed(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    for plan_id, effective_on in [
        ("pro", date(2026, 1, 12)),
        ("basic", date(2026, 1, 11)),
        ("basic", date(2026, 1, 10)),
        ("missing", date(2026, 1, 12)),
    ]:
        with pytest.raises((ValueError, KeyError)):
            billing.change_plan("s1", plan_id, effective_on)
        assert store.get_subscription("s1") == before
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000
    assert store.get_subscription("s1").plan_id == "basic"


def test_each_segment_rounds_half_up_independently(billing, store):
    subscription = store.get_subscription("s1")
    subscription.period_end = date(2026, 1, 3)
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [1, 2]


def test_included_usage_and_zero_price_overage(billing, store):
    store.get_plan("basic").included_units = 3
    billing.record_usage("s1", "included", 3, date(2026, 1, 1))
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]
    billing.record_usage("s1", "overage", 4, date(2026, 2, 1))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4520
