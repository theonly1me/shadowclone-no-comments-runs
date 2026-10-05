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
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_per_subscription_and_survives_service_restart(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 7
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 2)) is True
    billing = BillingService(store)
    assert billing.record_usage("s1", "event", 0, None) is False
    assert billing.record_usage("s2", "event", 5, date(2026, 1, 2)) is True
    assert billing.generate_invoice("s1").total_cents == 3021
    assert billing.generate_invoice("s2").total_cents == 3035
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


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


def test_late_and_future_usage_billed_once_at_half_open_boundaries(billing, store):
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


def test_multiple_changes_usage_allocation_and_next_period(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 11
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 6000, included_units=14, overage_unit_price_cents=9))
    store.add_plan(Plan("max", "Max", 9000, included_units=17, overage_unit_price_cents=11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    assert store.get_subscription("s1").plan_id == "max"
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 1)),
        ("basic", 3, date(2026, 1, 10)),
        ("pro-boundary", 6, date(2026, 1, 11)),
        ("max-boundary", 8, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 14),
        ("overage", "Overage: Pro", 18),
        ("overage", "Overage: Max", 33),
    ]
    assert invoice.total_cents == 6065
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert (subscription.period_start, subscription.period_end) == (
        date(2026, 1, 31), date(2026, 3, 2)
    )
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Max", 9000)]


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 1)),
    ("basic", date(2026, 1, 31)),
    ("basic", date(2025, 12, 31)),
    ("basic", date(2026, 2, 1)),
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 9)),
    ("pro", date(2026, 1, 20)),
])
def test_rejected_changes_are_atomic(billing, store, new_plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_can_return_to_original_plan_and_change_in_next_period(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000
    billing.change_plan("s1", "pro", date(2026, 2, 1))
    assert store.get_subscription("s1").plan_id == "pro"


def test_segment_prices_round_half_up_independently(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2)
    ]


def test_usage_allowance_does_not_transfer_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=10, overage_unit_price_cents=5))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 6, date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1"))[-1] == ("overage", "Overage: Pro", 5)


@pytest.mark.parametrize("credit,expected_credit,expected_tax,total", [
    (500, 500, 265, 2915),
    (5000, 3150, 0, 0),
])
def test_usage_subtotal_discount_credit_and_tax(
    billing, store, credit, expected_credit, expected_tax, total
):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 5, date(2026, 1, 2))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = credit
    invoice = billing.generate_invoice("s1", "TEN")
    expected = [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
        ("discount", "Discount", -350),
        ("credit", "Account credit", -expected_credit),
    ]
    if expected_tax:
        expected.append(("tax", "Tax", expected_tax))
    assert lines(invoice) == expected
    assert invoice.total_cents == total
    assert customer.credit_balance_cents == credit - expected_credit


def test_zero_price_overage_line_is_present(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1"))[-1] == ("overage", "Overage: Basic", 0)


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4510
