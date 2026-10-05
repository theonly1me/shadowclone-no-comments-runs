from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def invoice_lines(invoice):
    return [
        (item.kind, item.description, item.amount_cents)
        for item in invoice.line_items
    ]


def test_plan_usage_defaults():
    plan = Plan("default", "Default", 1000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_per_subscription(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "event", 2, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2030, 1, 1)) is False
    assert billing.record_usage("s2", "event", 3, date(2026, 1, 1)) is True
    assert billing.generate_invoice("s1").total_cents == 3020
    assert billing.generate_invoice("s2").total_cents == 3030
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_leaves_no_event(billing, store, units):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert subscription == before
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


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
    billing.record_usage("s1", "late-after-close", 6, date(2026, 1, 5))
    assert BillingService(store).generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_segment_usage_and_line_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=11))
    store.add_plan(Plan("premium", "Premium", 9000, included_units=30, overage_unit_price_cents=13))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "premium", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 1)),
        ("basic", 3, date(2026, 1, 10)),
        ("pro-start", 4, date(2026, 1, 11)),
        ("pro-end", 4, date(2026, 1, 20)),
        ("premium-start", 12, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    invoice = billing.generate_invoice("s1")

    assert invoice_lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Premium", 3000),
        ("overage", "Overage: Basic", 14),
        ("overage", "Overage: Pro", 22),
        ("overage", "Overage: Premium", 26),
    ]
    assert invoice.total_cents == 6062
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []
    assert invoice_lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Premium", 9000)
    ]


def test_proration_rounds_each_exact_half_up_and_changes_reset(billing, store):
    store.get_plan("basic").monthly_price_cents = 1001
    store.add_plan(Plan("pro", "Pro", 2001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    assert invoice_lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 501),
        ("plan", "Plan: Pro", 1001),
    ]
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert invoice_lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Pro", 67),
        ("plan", "Plan: Basic", 968),
    ]


@pytest.mark.parametrize(
    "plan_id,effective_on,error",
    [
        ("missing", date(2026, 1, 20), KeyError),
        ("basic", date(2026, 1, 1), ValueError),
        ("basic", date(2026, 1, 31), ValueError),
        ("basic", date(2025, 12, 31), ValueError),
        ("basic", date(2026, 2, 1), ValueError),
        ("basic", date(2026, 1, 9), ValueError),
        ("basic", date(2026, 1, 10), ValueError),
        ("pro", date(2026, 1, 20), ValueError),
    ],
)
def test_rejected_changes_leave_state_unchanged(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)

    assert subscription == before


def test_change_to_initial_current_plan_is_rejected(billing, store):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    assert subscription == before


def test_overage_discount_credit_and_tax(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 100
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    billing.record_usage("s1", "usage", 15, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", "TEN")

    assert invoice_lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
        ("discount", "Discount", -350),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_credit_and_discount_caps_include_overage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 4000
    customer.tax_rate_percent = Decimal("10")
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=500))
    billing.record_usage("s1", "first", 2, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1", "FIXED")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 200), ("discount", -500), ("credit", -2700)
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1300
    store.add_discount_code(DiscountCode("FREE", amount_off_cents=10000))
    billing.record_usage("s1", "second", 3, date(2026, 2, 1))
    invoice = billing.generate_invoice("s1", "FREE")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 300), ("discount", -3300)
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1300


def test_failed_invoice_does_not_consume_usage_or_plan_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=100))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 4700


def test_zero_price_overage_line_is_kept(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert invoice_lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]


def test_unused_allowances_do_not_transfer_between_segments(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 30
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=30, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "first", 10, date(2026, 1, 1))
    billing.record_usage("s1", "last", 15, date(2026, 1, 21))

    assert invoice_lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 50),
    ]
