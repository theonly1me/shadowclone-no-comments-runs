from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10

    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, None) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))

    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription_usage_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 1)) is True


def test_usage_boundaries_late_events_and_billing_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    for event_id, units, occurred_on in [
        ("late", 1, date(2025, 12, 1)),
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


@pytest.mark.parametrize("units, expected", [(9, 3000), (10, 3000), (11, 3007)])
def test_included_usage_without_changes(billing, store, units, expected):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "event", units, date(2026, 1, 10))

    assert billing.generate_invoice("s1").total_cents == expected


def test_multiple_changes_usage_and_adjustments(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 9
    basic.overage_unit_price_cents = 5
    store.add_plan(Plan("plus", "Plus", 6000, included_units=6, overage_unit_price_cents=10))
    store.add_plan(Plan("pro", "Pro", 9000, included_units=3, overage_unit_price_cents=20))
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 21))
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))
    billing.record_usage("s1", "basic", 5, date(2026, 1, 10))
    billing.record_usage("s1", "plus", 4, date(2026, 1, 11))
    billing.record_usage("s1", "pro", 3, date(2026, 1, 21))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = BillingService(store).generate_invoice("s1", "TEN")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Plus", 2000),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Plus", 20),
        ("overage", "Overage: Pro", 40),
        ("discount", "Discount", -608),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 497),
    ]
    assert invoice.total_cents == 5469
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) is invoice
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    customer.tax_rate_percent = Decimal("0")
    assert billing.generate_invoice("s1").total_cents == 9000


def test_segment_rounding_and_included_units_floor(billing, store):
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 1
    basic.included_units = 5
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("plus", "Plus", 1, included_units=5, overage_unit_price_cents=9))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "first", 3, date(2026, 1, 15))
    billing.record_usage("s1", "second", 3, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1), ("plan", 1), ("overage", 7), ("overage", 9)
    ]


@pytest.mark.parametrize(
    "plan_id, effective_on, error",
    [
        ("basic", date(2026, 1, 15), ValueError),
        ("plus", date(2026, 1, 1), ValueError),
        ("plus", date(2026, 1, 31), ValueError),
        ("plus", date(2025, 12, 31), ValueError),
        ("plus", date(2026, 2, 1), ValueError),
        ("missing", date(2026, 1, 15), KeyError),
    ],
)
def test_rejected_first_change_leaves_state_unchanged(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("plus", "Plus", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)

    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("day", [5, 10])
def test_changes_must_be_strictly_chronological(billing, store, day):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, day))

    assert subscription == before


def test_returning_to_original_plan_and_changing_in_next_period(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", date(2026, 1, 15))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    assert billing.generate_invoice("s1").total_cents == 4000
    assert store.get_subscription("s1").plan_id == "basic"
    billing.change_plan("s1", "plus", date(2026, 2, 1))
    assert billing.generate_invoice("s1").total_cents == 5900


def test_unknown_subscription_plan_change(billing):
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 15))


def test_zero_price_overage_is_still_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0)
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 4520
