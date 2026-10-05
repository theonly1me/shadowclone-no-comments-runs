from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def charges(invoice):
    return [
        (item.kind, item.description, item.amount_cents)
        for item in invoice.line_items
    ]


def test_plan_usage_defaults():
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_with_invalid_duplicate(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 2)) is False
    invoice = BillingService(store).generate_invoice("s1")
    assert invoice.total_cents == 3030
    assert billing.record_usage("s1", "event", -1, date(2026, 1, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_store_event(billing, store, units):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert store.get_subscription("s1") == before
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


def test_usage_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 2)) is True


def test_usage_boundaries_late_events_and_future_events(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 2
    plan.overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 2, date(2026, 1, 1))
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))
    billing.record_usage("s1", "end", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 5, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3030
    billing.record_usage("s1", "new-late", 1, date(2026, 1, 1))
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_segment_usage_and_adjustment_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=11))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))
    billing.record_usage("s1", "first", 2, date(2026, 1, 10))
    billing.record_usage("s1", "second", 8, date(2026, 1, 11))
    billing.record_usage("s1", "third", 6, date(2026, 1, 21))

    invoice = billing.generate_invoice("s1", "TEN")

    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 28),
        ("overage", "Overage: Pro", 22),
        ("overage", "Overage: Basic", 21),
        ("discount", "Discount", -407),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 316),
    ]
    assert invoice.total_cents == 3480
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_proration_rounds_each_segment_half_up_and_keeps_final_plan(billing, store):
    store.get_plan("basic").monthly_price_cents = 1001
    store.add_plan(Plan("pro", "Pro", 2001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 501),
        ("plan", "Plan: Pro", 1001),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 2001)]
    billing.change_plan("s1", "basic", date(2026, 3, 3))


@pytest.mark.parametrize("effective_on", [
    date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1),
])
def test_change_must_be_strictly_inside_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


def test_changes_are_ordered_and_cannot_repeat_current_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    assert store.get_subscription("s1") == before
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    before = deepcopy(store.get_subscription("s1"))
    for plan_id, effective_on in [
        ("pro", date(2026, 1, 20)),
        ("basic", date(2026, 1, 15)),
        ("basic", date(2026, 1, 14)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
        assert store.get_subscription("s1") == before


def test_zero_price_overage_line_is_present(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


@pytest.mark.parametrize("units", [2, 3])
def test_usage_within_allowance_has_no_overage_line(billing, store, units):
    plan = store.get_plan("basic")
    plan.included_units = 3
    plan.overage_unit_price_cents = 10
    billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_credit_is_capped_after_usage_discount(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=1000))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 100, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "FIXED")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -1000), ("credit", -3000),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4520
