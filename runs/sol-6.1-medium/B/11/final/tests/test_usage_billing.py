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
def test_invalid_usage_is_not_recorded(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2))


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_usage_idempotency_is_per_subscription_and_survives_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 2, date(2026, 1, 2))
    assert not billing.record_usage("s1", "event", 0, date(2026, 4, 1))
    assert billing.record_usage("s2", "event", 3, date(2026, 1, 2))
    assert BillingService(store).generate_invoice("s1").total_cents == 3020
    assert not billing.record_usage("s1", "event", -1, date(2026, 1, 2))
    assert billing.generate_invoice("s1").total_cents == 3000
    assert billing.generate_invoice("s2").total_cents == 3030


def test_usage_aggregates_before_subtracting_allowance(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    billing.record_usage("s1", "one", 6, date(2026, 1, 1))
    billing.record_usage("s1", "two", 7, date(2026, 1, 30))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 75)
    ]


def test_late_and_future_usage_bill_once_when_eligible(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 1, date(2025, 12, 1))
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010
    billing.record_usage("s1", "new-late", 4, date(2026, 1, 10))
    assert billing.generate_invoice("s1").total_cents == 3060
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_attribution_proration_and_next_period(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 8
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=11, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))
    billing.record_usage("s1", "first-end", 2, date(2026, 1, 10))
    billing.record_usage("s1", "second-start", 5, date(2026, 1, 11))
    billing.record_usage("s1", "third-start", 4, date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 30),
        ("overage", "Overage: Pro", 40),
        ("overage", "Overage: Basic", 20),
    ]
    assert invoice.total_cents == 4090
    assert store.get_invoice(invoice.invoice_id) is invoice
    assert (invoice.period_start, invoice.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_final_plan_carries_forward_and_change_order_resets(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 30))
    billing.generate_invoice("s1")
    assert store.get_subscription("s1").plan_id == "pro"
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [200, 2900]


@pytest.mark.parametrize("effective_on", [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 1)])
def test_change_must_be_inside_open_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 3000


def test_rejected_changes_preserve_all_state(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    original_changes = list(subscription.plan_changes)
    for effective_on in [date(2026, 1, 15), date(2026, 1, 16)]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", "basic", effective_on)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 17))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 17))
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == original_changes
    assert subscription.period_start == date(2026, 1, 1)
    assert subscription.period_end == date(2026, 1, 31)
    assert billing.generate_invoice("s1").total_cents == 4500


def test_plan_proration_rounds_each_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [1, 2]


def test_discount_credit_and_tax_include_overage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 10, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_discount_and_credit_caps_with_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("FLAT", amount_off_cents=3500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 1000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 10, date(2026, 1, 2))
    assert [(item.kind, item.amount_cents) for item in billing.generate_invoice("s1", "FLAT").line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -3500), ("credit", -500)
    ]
    assert customer.credit_balance_cents == 500
    assert [(item.kind, item.amount_cents) for item in billing.generate_invoice("s1", "FLAT").line_items] == [
        ("plan", 3000), ("discount", -3000)
    ]
    assert customer.credit_balance_cents == 500


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 5, date(2026, 1, 16))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert subscription.plan_id == "basic"
    assert len(subscription.plan_changes) == 1
    assert billing.generate_invoice("s1").total_cents == 4550
