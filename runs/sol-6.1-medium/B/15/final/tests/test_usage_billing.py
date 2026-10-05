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


def test_usage_is_idempotent_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "event", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, None) is False
    assert billing.record_usage("s2", "event", 10, date(2026, 1, 3)) is True
    event = store.get_subscription("s1").usage_events["event"]
    assert (event.units, event.occurred_on) == (5, date(2026, 1, 2))


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_no_event(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert store.get_subscription("s1").usage_events == {}


def test_unknown_ids(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_usage_boundaries_late_events_and_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    for event_id, units, occurred_on in [
        ("late", 1, date(2025, 12, 1)),
        ("start", 2, date(2026, 1, 1)),
        ("last", 3, date(2026, 1, 30)),
        ("next", 4, date(2026, 1, 31)),
        ("future", 5, date(2026, 3, 2)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 60)
    ]
    assert billing.record_usage("s1", "late", -5, None) is False
    billing.record_usage("s1", "late-arrival", 6, date(2026, 1, 10))
    assert lines(BillingService(store).generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 100)
    ]
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 50)
    ]
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_included_units_are_aggregated_and_reset(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "a", 6, date(2026, 1, 2))
    billing.record_usage("s1", "b", 6, date(2026, 1, 3))
    assert billing.generate_invoice("s1").total_cents == 3014
    billing.record_usage("s1", "c", 10, date(2026, 2, 1))
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)])
def test_change_must_be_strictly_within_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


def test_changes_validate_current_plan_and_chronology(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    for new_plan_id, effective_on in [
        ("pro", date(2026, 1, 12)),
        ("basic", date(2026, 1, 11)),
        ("basic", date(2026, 1, 10)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", new_plan_id, effective_on)
        assert store.get_subscription("s1") == before
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000


def test_multiple_segments_usage_floor_rounding_and_adjustments(billing, store):
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 1001
    basic.included_units = 10
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 2001, included_units=20, overage_unit_price_cents=20))
    store.add_plan(Plan("max", "Max", 3001, included_units=30, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 8, date(2026, 1, 11)),
        ("third", 11, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 100
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 334),
        ("plan", "Plan: Pro", 667),
        ("plan", "Plan: Max", 1000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 40),
        ("overage", "Overage: Max", 30),
        ("discount", "Discount", -209),
        ("credit", "Account credit", -100),
        ("tax", "Tax", 178),
    ]
    assert invoice.total_cents == 1960
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert (subscription.period_start, subscription.period_end) == (date(2026, 1, 31), date(2026, 3, 2))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Max", 3001), ("tax", "Tax", 300)
    ]
    billing.change_plan("s1", "basic", date(2026, 3, 3))


def test_half_up_segment_prices_and_zero_price_overage(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 1, date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1),
        ("plan", "Plan: Pro", 2),
        ("overage", "Overage: Pro", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 5, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4550


def test_credit_cap_includes_overage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.get_customer("c1").credit_balance_cents = 5000
    store.get_customer("c1").tax_rate_percent = Decimal("10")
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=100))
    billing.record_usage("s1", "event", 20, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 200), ("discount", -100), ("credit", -3100)
    ]
    assert invoice.total_cents == 0
    assert store.get_customer("c1").credit_balance_cents == 1900
