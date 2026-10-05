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


def test_usage_idempotency_survives_billing_and_service_recreation(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 5, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3050

    billing = BillingService(store)
    assert billing.record_usage("s1", "event", -1, date(2025, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 1)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_consume_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription_usage(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))


def test_future_and_late_usage_billed_once_on_next_eligible_invoice(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "last", 2, date(2026, 1, 30))
    billing.record_usage("s1", "boundary", 3, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3030

    billing.record_usage("s1", "late", 5, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3080
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_boundary_attribution_and_adjustment_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 9
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=12, overage_unit_price_cents=20))
    store.add_plan(Plan("max", "Max", 9000, included_units=15, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "late", 5, date(2025, 12, 1))
    billing.record_usage("s1", "pro", 7, date(2026, 1, 11))
    billing.record_usage("s1", "max", 9, date(2026, 1, 21))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "TEN")
    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Max", 120),
        ("discount", "Discount", -620),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 508),
    ]
    assert invoice.total_cents == 5588
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) is invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Max", 9000), ("tax", "Tax", 900)
    ]


def test_segment_rounding_and_included_units_floor(billing, store):
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 1
    basic.included_units = 3
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 3, included_units=3, overage_unit_price_cents=11))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 15))
    billing.record_usage("s1", "second", 2, date(2026, 1, 16))

    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1),
        ("plan", "Plan: Pro", 2),
        ("overage", "Overage: Basic", 7),
        ("overage", "Overage: Pro", 11),
    ]


def test_unused_allowance_is_not_shared_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=10, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "pro", 7, date(2026, 1, 16))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Pro", 20),
    ]


def test_usage_within_allowance_has_no_overage_line(billing, store):
    store.get_plan("basic").included_units = 5
    billing.record_usage("s1", "usage", 5, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_positive_overage_units_have_a_line_even_with_zero_price(billing):
    billing.record_usage("s1", "usage", 1, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]


@pytest.mark.parametrize("effective_on", [
    date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1),
])
def test_change_must_be_strictly_inside_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("plan_id,effective_on,exception", [
    ("pro", date(2026, 1, 21), ValueError),
    ("basic", date(2026, 1, 11), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("missing", date(2026, 1, 21), KeyError),
])
def test_rejected_changes_are_atomic(billing, store, plan_id, effective_on, exception):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(exception):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_returning_to_initial_plan_and_changes_in_next_period(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    BillingService(store).change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    assert billing.generate_invoice("s1").total_cents == 5000


def test_credit_caps_at_subtotal_including_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "usage", 5, date(2026, 1, 1))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
        ("credit", "Account credit", -3050),
    ]
    assert customer.credit_balance_cents == 1950


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "usage", 5, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4550
