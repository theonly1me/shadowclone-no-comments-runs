from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def items(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("plain", "Plain", 1000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_aggregated_and_included_units_are_not_charged(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    assert billing.record_usage("s1", "a", 6, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "b", 7, date(2026, 1, 30)) is True

    invoice = billing.generate_invoice("s1")

    assert items(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 75),
    ]
    assert invoice.total_cents == 3075
    assert items(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_duplicate_usage_is_ignored_before_validation_and_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 2, date(2026, 1, 2)) is True
    assert BillingService(store).record_usage("s1", "event", -1, None) is False
    assert billing.generate_invoice("s1").total_cents == 3020
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True


def test_unknown_subscriptions_and_plans(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert subscription == before


def test_usage_is_idempotent_per_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    store.get_plan("basic").overage_unit_price_cents = 5
    assert billing.record_usage("s1", "same", 2, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "same", 3, date(2026, 1, 1)) is True
    assert billing.generate_invoice("s1").total_cents == 3010
    assert billing.generate_invoice("s2").total_cents == 3015


def test_future_and_late_events_are_billed_on_the_next_eligible_invoice(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    billing.record_usage("s1", "late", 4, date(2025, 12, 31))
    assert billing.generate_invoice("s1").total_cents == 3040
    billing.record_usage("s1", "closed-period", 5, date(2026, 1, 2))
    assert billing.generate_invoice("s1").total_cents == 3070
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_attribute_boundaries_and_late_usage(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=8, overage_unit_price_cents=10))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=11, overage_unit_price_cents=20))
    store.add_plan(Plan("plus", "Plus", 9000, included_units=14, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    BillingService(store).change_plan("s1", "plus", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 3, date(2025, 12, 1)),
        ("start", 1, date(2026, 1, 1)),
        ("before", 1, date(2026, 1, 10)),
        ("first-change", 7, date(2026, 1, 11)),
        ("second-change", 9, date(2026, 1, 21)),
        ("future", 15, date(2026, 1, 31)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    invoice = BillingService(store).generate_invoice("s1")

    assert items(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 30),
        ("overage", "Overage: Pro", 80),
        ("overage", "Overage: Plus", 150),
    ]
    assert invoice.total_cents == 6260
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Plus", 9000),
        ("overage", "Overage: Plus", 30),
    ]


@pytest.mark.parametrize("effective_on", [
    date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1),
])
def test_changes_must_be_strictly_inside_the_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert subscription == before


def test_changes_must_be_increasing_and_differ_from_current_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    assert subscription == before
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(subscription)
    for plan_id, effective_on in [
        ("basic", date(2026, 1, 10)),
        ("basic", date(2026, 1, 11)),
        ("pro", date(2026, 1, 20)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
        assert subscription == before
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [1000, 2000, 1000]
    assert subscription.plan_id == "basic"
    billing.change_plan("s1", "pro", date(2026, 2, 1))


def test_proration_rounds_each_segment_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 1001
    store.add_plan(Plan("pro", "Pro", 2001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [501, 1001]
    assert invoice.total_cents == 1502


def test_overage_precedes_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 25
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "usage", 10, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 250), ("discount", -325),
        ("credit", -500), ("tax", 243),
    ]
    assert invoice.total_cents == 2668
    assert customer.credit_balance_cents == 0


def test_credit_is_capped_after_discount_including_overage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=100))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "usage", 20, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "FIXED")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 200), ("discount", -100), ("credit", -3100),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1900


def test_zero_price_overage_line_is_still_present(billing):
    billing.record_usage("s1", "usage", 1, date(2026, 1, 1))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "usage", 1, date(2026, 1, 1))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert subscription == before
    assert billing.generate_invoice("s1").invoice_id == "inv-1"
