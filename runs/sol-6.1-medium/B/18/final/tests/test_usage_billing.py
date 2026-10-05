from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def test_plan_usage_defaults():
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_aggregated_and_billed_once(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    assert billing.record_usage("s1", "a", 8, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "b", 7, date(2026, 1, 30)) is True
    invoice = BillingService(store).generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 125)
    ]
    assert billing.generate_invoice("s1").total_cents == 3000
    assert billing.record_usage("s1", "a", 0, date(2030, 1, 1)) is False


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "same", -1, date(2026, 2, 2)) is False


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "a", units, date(2026, 1, 2))
    assert billing.record_usage("s1", "a", 1, date(2026, 1, 2)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "a", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_future_and_late_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3000
    billing.record_usage("s1", "late", 4, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3060
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_segment_usage_and_final_plan(billing, store):
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=20))
    store.add_plan(Plan("max", "Max", 9000, included_units=30, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 4, date(2025, 12, 31)),
        ("first", 1, date(2026, 1, 10)),
        ("second", 8, date(2026, 1, 11)),
        ("third", 11, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 40),
        ("overage", "Overage: Max", 30),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 9000
    billing.change_plan("s1", "basic", date(2026, 3, 3))


@pytest.mark.parametrize("new_plan, effective_on, error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 15), ValueError),
    ("missing", date(2026, 1, 15), KeyError),
])
def test_rejected_initial_change_is_atomic(billing, store, new_plan, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", new_plan, effective_on)
    assert subscription == before


@pytest.mark.parametrize("new_plan, day", [("basic", 10), ("basic", 9), ("pro", 11)])
def test_rejected_subsequent_change_is_atomic(billing, store, new_plan, day):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan, date(2026, 1, day))
    assert subscription == before


def test_plan_proration_rounds_each_segment_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 1001
    store.add_plan(Plan("pro", "Pro", 2001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [501, 1001]


def test_usage_subtotal_discount_credit_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 25
    billing.record_usage("s1", "a", 20, date(2026, 1, 15))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 500), ("discount", -350),
        ("credit", -500), ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice


def test_zero_price_overage_still_has_line(billing):
    billing.record_usage("s1", "a", 1, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0)
    ]


def test_change_back_to_starting_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1000, 2000, 1000]
    assert store.get_subscription("s1").plan_id == "basic"


def test_credit_is_capped_after_discount_on_usage_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "a", 5, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 4000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 500), ("discount", -500), ("credit", -3000)
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1000
