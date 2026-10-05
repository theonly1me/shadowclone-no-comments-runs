from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def test_plan_usage_defaults(store):
    plan = store.get_plan("basic")
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_with_invalid_replacement(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 5)) is False
    assert BillingService(store).record_usage("s1", "event", -1, None) is False
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.record_usage("s1", "event", 100, date(2026, 2, 5)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_record_event(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 5))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 5))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 5))
    assert store.get_subscription("s1") == before


def test_usage_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 5)) is True


def test_future_and_late_events_bill_once_in_next_eligible_period(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 2, date(2026, 1, 1))
    billing.record_usage("s1", "end", 3, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3020
    billing.record_usage("s1", "late", 5, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3080
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_included_units_are_aggregated_across_events(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "one", 6, date(2026, 1, 3))
    billing.record_usage("s1", "two", 7, date(2026, 1, 4))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 21)
    ]


def test_multiple_changes_usage_boundaries_and_late_events(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 10
    store.add_plan(Plan("premium", "Premium", 6000, 20, 20))
    billing.change_plan("s1", "premium", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_id == "premium"
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))
    billing.record_usage("s1", "first", 7, date(2026, 1, 10))
    billing.record_usage("s1", "second", 9, date(2026, 1, 11))
    billing.record_usage("s1", "third", 8, date(2026, 1, 21))
    invoice = BillingService(store).generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents)
            for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Premium", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 90),
        ("overage", "Overage: Premium", 60),
        ("overage", "Overage: Basic", 50),
    ]
    assert invoice.total_cents == 4200
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert billing.generate_invoice("s1").total_cents == 3000


def test_segment_rounding_allowance_floor_and_next_period_plan(billing, store):
    store.add_plan(Plan("premium", "Premium", 6001, 3, 10))
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 3001
    basic.included_units = 3
    basic.overage_unit_price_cents = 10
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 15))
    billing.record_usage("s1", "second", 2, date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1501, 3001, 10, 10]
    assert store.get_subscription("s1").plan_id == "premium"
    billing.change_plan("s1", "basic", date(2026, 2, 15))
    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [
        3001, 1501
    ]


@pytest.mark.parametrize("effective_on", [
    date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)
])
def test_plan_change_must_be_strictly_inside_period(billing, store, effective_on):
    store.add_plan(Plan("premium", "Premium", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", effective_on)
    assert store.get_subscription("s1") == before


def test_rejected_changes_preserve_state(billing, store):
    store.add_plan(Plan("premium", "Premium", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    billing.change_plan("s1", "premium", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    for plan_id, effective_on in [
        ("basic", date(2026, 1, 9)),
        ("basic", date(2026, 1, 10)),
        ("premium", date(2026, 1, 11)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
        assert store.get_subscription("s1") == before


def test_discount_credit_and_tax_include_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "event", 10, date(2026, 1, 5))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice


def test_failed_invoice_does_not_consume_usage_or_clear_changes(billing, store):
    store.add_plan(Plan("premium", "Premium", 6000, 0, 10))
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "event", 5, date(2026, 1, 20))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4550


def test_positive_overage_with_zero_price_still_has_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0)
    ]
