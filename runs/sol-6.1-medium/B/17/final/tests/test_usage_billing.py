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


def test_usage_is_idempotent_even_with_invalid_replacement(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10

    assert billing.record_usage("s1", "event", 3, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 1)) is False
    assert billing.record_usage("s1", "event", 99, date(2026, 1, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_no_event(billing, store, units):
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))

    assert store.get_subscription("s1") == before
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    store.get_plan("basic").overage_unit_price_cents = 10

    assert billing.record_usage("s1", "event", 2, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "event", 3, date(2026, 1, 2)) is True
    assert billing.generate_invoice("s1").total_cents == 3020
    assert billing.generate_invoice("s2").total_cents == 3030


def test_usage_half_open_periods_and_late_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))

    assert billing.generate_invoice("s1").total_cents == 3010
    billing.record_usage("s1", "late", 3, date(2026, 1, 15))
    assert BillingService(store).generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units, expected", [(4, 3000), (5, 3000), (8, 3021)])
def test_full_period_included_units(billing, store, units, expected):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "event", units, date(2026, 1, 2))

    assert billing.generate_invoice("s1").total_cents == expected


def test_segments_round_half_up_and_floor_allowances(billing, store):
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 3001
    basic.included_units = 5
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6001, included_units=9, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "late", 1, date(2025, 12, 31))
    billing.record_usage("s1", "before", 5, date(2026, 1, 15))
    billing.record_usage("s1", "boundary", 5, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1501),
        ("plan", "Plan: Pro", 3001),
        ("overage", "Overage: Basic", 40),
        ("overage", "Overage: Pro", 20),
    ]
    assert invoice.total_cents == 4562
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) is invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert billing.generate_invoice("s1").total_cents == 6001


def test_multiple_changes_can_return_to_original_plan(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 30
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    BillingService(store).change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "first", 11, date(2026, 1, 10))
    billing.record_usage("s1", "second", 22, date(2026, 1, 11))
    billing.record_usage("s1", "third", 13, date(2026, 1, 21))

    invoice = BillingService(store).generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Basic", 1000),
        ("Plan: Pro", 2000),
        ("Plan: Basic", 1000),
        ("Overage: Basic", 10),
        ("Overage: Pro", 40),
        ("Overage: Basic", 30),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "plan_id, effective_on, error",
    [
        ("basic", date(2026, 1, 1), ValueError),
        ("basic", date(2025, 12, 31), ValueError),
        ("basic", date(2026, 1, 31), ValueError),
        ("basic", date(2026, 2, 1), ValueError),
        ("basic", date(2026, 1, 10), ValueError),
        ("basic", date(2026, 1, 9), ValueError),
        ("pro", date(2026, 1, 20), ValueError),
        ("missing", date(2026, 1, 20), KeyError),
    ],
)
def test_rejected_changes_leave_all_state_unchanged(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1") == before


def test_change_to_current_plan_without_prior_changes_is_rejected(billing, store):
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    assert store.get_subscription("s1") == before


def test_change_history_resets_for_next_period(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 30))
    billing.generate_invoice("s1")
    billing.change_plan("s1", "basic", date(2026, 2, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Pro", 200),
        ("Plan: Basic", 2900),
    ]


def test_usage_subtotal_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "basic", 3, date(2026, 1, 1))
    billing.record_usage("s1", "pro", 1, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 3000),
        ("overage", 300),
        ("overage", 200),
        ("discount", -500),
        ("credit", -500),
        ("tax", 400),
    ]
    assert invoice.total_cents == 4400
    assert customer.credit_balance_cents == 0


def test_credit_capped_after_discount_on_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 2, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1", "FIXED")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 200), ("discount", -500), ("credit", -2700),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2300


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4520
