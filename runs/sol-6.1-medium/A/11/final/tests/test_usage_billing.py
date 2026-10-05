from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def charges(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_record_event(billing, store, units):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert store.get_subscription("s1") == before
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_usage_idempotency_persists_and_is_per_subscription(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 2, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 1)) is False
    assert billing.record_usage("s2", "event", 3, date(2026, 1, 1)) is True
    billing = BillingService(store)
    assert billing.generate_invoice("s1").total_cents == 3200
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000
    assert billing.generate_invoice("s2").total_cents == 3300


def test_future_and_late_usage_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3100
    billing.record_usage("s1", "late", 4, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3600
    assert billing.generate_invoice("s1").total_cents == 3300
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_segment_allowances_and_boundaries(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 31
    basic.overage_unit_price_cents = 2
    store.add_plan(Plan("pro", "Pro", 6000, included_units=62, overage_unit_price_cents=3))
    store.add_plan(Plan("max", "Max", 9000, included_units=93, overage_unit_price_cents=4))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "late", 12, date(2025, 12, 31))
    billing.record_usage("s1", "pro-start", 25, date(2026, 1, 11))
    billing.record_usage("s1", "max-start", 32, date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 4),
        ("overage", "Overage: Pro", 15),
        ("overage", "Overage: Max", 4),
    ]
    assert invoice.total_cents == 6023
    assert store.get_invoice(invoice.invoice_id) is invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert (subscription.period_start, subscription.period_end) == (
        date(2026, 1, 31), date(2026, 3, 2)
    )
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Max", 9000)]
    # Ordering validation resets with each new period.
    billing.change_plan("s1", "basic", date(2026, 3, 3))


def test_each_segment_rounds_half_up(billing, store):
    subscription = store.get_subscription("s1")
    subscription.period_end = date(2026, 1, 3)
    store.get_plan("basic").monthly_price_cents = 5
    store.add_plan(Plan("pro", "Pro", 7))
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3), ("plan", "Plan: Pro", 4)
    ]


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("missing", date(2026, 1, 20), KeyError),
    ("pro", date(2026, 1, 20), ValueError),
    ("basic", date(2026, 1, 1), ValueError),
    ("basic", date(2025, 12, 31), ValueError),
    ("basic", date(2026, 1, 31), ValueError),
    ("basic", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 11), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
])
def test_rejected_changes_leave_state_unchanged(billing, store, new_plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_change_back_to_original_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_usage_discount_credit_tax(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 20, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 600
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -600), ("tax", 300),
    ]
    assert invoice.total_cents == 3300
    assert customer.credit_balance_cents == 0


def test_included_usage_has_no_overage_line(billing, store):
    store.get_plan("basic").included_units = 10
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]
