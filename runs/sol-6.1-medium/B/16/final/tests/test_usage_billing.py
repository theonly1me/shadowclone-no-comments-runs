from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10

    assert billing.record_usage("s1", "event", 3, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", 99, date(2026, 2, 2)) is False
    assert BillingService(store).generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))

    assert subscription == before
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


def test_usage_boundaries_late_events_and_future_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "last", 2, date(2026, 1, 30))
    billing.record_usage("s1", "end", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 8, date(2026, 3, 2))

    assert billing.generate_invoice("s1").total_cents == 3030
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3070
    assert billing.generate_invoice("s1").total_cents == 3080
    assert billing.generate_invoice("s1").total_cents == 3000


def test_included_units_apply_to_aggregate_usage(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 20
    billing.record_usage("s1", "one", 3, date(2026, 1, 1))
    billing.record_usage("s1", "two", 4, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 40),
    ]
    assert invoice.total_cents == 3040


def test_usage_within_allowance_has_no_overage_line(billing, store):
    store.get_plan("basic").included_units = 5
    billing.record_usage("s1", "event", 5, date(2026, 1, 1))

    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]


def test_multiple_changes_proration_usage_attribution_and_rollover(billing, store):
    store.add_plan(Plan("basic", "Basic", 3001, included_units=10, overage_unit_price_cents=7))
    store.add_plan(Plan("plus", "Plus", 6003, included_units=11, overage_unit_price_cents=11))
    store.add_plan(Plan("pro", "Pro", 9001, included_units=7, overage_unit_price_cents=13))
    events = [
        ("late", 2, date(2025, 12, 31)),
        ("start", 1, date(2026, 1, 1)),
        ("before_plus", 3, date(2026, 1, 10)),
        ("plus_start", 3, date(2026, 1, 11)),
        ("before_pro", 1, date(2026, 1, 15)),
        ("pro_start", 5, date(2026, 1, 16)),
        ("last", 1, date(2026, 1, 30)),
    ]
    for event_id, units, occurred_on in events:
        billing.record_usage("s1", event_id, units, occurred_on)
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    BillingService(store).change_plan("s1", "pro", date(2026, 1, 16))

    invoice = BillingService(store).generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Plus", 1001),
        ("plan", "Plan: Pro", 4501),
        ("overage", "Overage: Basic", 21),
        ("overage", "Overage: Plus", 33),
        ("overage", "Overage: Pro", 39),
    ]
    assert invoice.total_cents == 6595
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert billing.generate_invoice("s1").total_cents == 9001
    billing.change_plan("s1", "basic", date(2026, 3, 3))


@pytest.mark.parametrize(
    ("plan_id", "effective_on"),
    [
        ("plus", date(2025, 12, 31)),
        ("plus", date(2026, 1, 1)),
        ("plus", date(2026, 1, 31)),
        ("plus", date(2026, 2, 1)),
        ("basic", date(2026, 1, 15)),
    ],
)
def test_rejected_initial_changes_are_atomic(billing, store, plan_id, effective_on):
    store.add_plan(Plan("plus", "Plus", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert subscription == before


@pytest.mark.parametrize(
    ("plan_id", "effective_on"),
    [
        ("basic", date(2026, 1, 9)),
        ("basic", date(2026, 1, 10)),
        ("plus", date(2026, 1, 11)),
    ],
)
def test_rejected_subsequent_changes_are_atomic(billing, store, plan_id, effective_on):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    before = deepcopy(subscription)

    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert subscription == before
    billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_usage_subtotal_discount_credit_and_tax_order(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("plus", "Plus", 6000, overage_unit_price_cents=200))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "basic", 2, date(2026, 1, 2))
    billing.record_usage("s1", "plus", 3, date(2026, 1, 16))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 3000),
        ("overage", 200),
        ("overage", 600),
        ("discount", -530),
        ("credit", -500),
        ("tax", 427),
    ]
    assert invoice.total_cents == 4697
    assert customer.credit_balance_cents == 0


def test_capped_discount_and_credit_with_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 2, date(2026, 1, 2))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=1000))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "OFF")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -1000),
        ("credit", -2200),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2800


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000, overage_unit_price_cents=100))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "event", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4600


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    store.get_plan("basic").overage_unit_price_cents = 100

    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 1)) is True
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s2").total_cents == 3200
