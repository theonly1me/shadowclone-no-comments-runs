from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def charges(invoice):
    return [
        (item.kind, item.description, item.amount_cents) for item in invoice.line_items
    ]


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


@pytest.mark.parametrize("units", [0, -1])
def test_usage_requires_positive_units(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))

    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1))


def test_unknown_subscription_for_usage_and_changes(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_usage_idempotency_is_per_subscription_and_survives_invoicing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "event", 2, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 100, date(2026, 1, 31)) is False
    assert billing.record_usage("s1", "event", 0, None) is False
    assert billing.record_usage("s2", "event", 3, date(2026, 1, 2)) is True
    assert billing.generate_invoice("s1").total_cents == 3020
    assert billing.generate_invoice("s2").total_cents == 3030
    assert billing.record_usage("s1", "event", -1, None) is False
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_is_billed_once_in_first_eligible_period(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))

    assert billing.generate_invoice("s1").total_cents == 3010
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_included_units_apply_to_aggregated_usage(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    billing.record_usage("s1", "one", 7, date(2026, 1, 1))
    billing.record_usage("s1", "two", 8, date(2026, 1, 30))

    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 125),
    ]


@pytest.mark.parametrize("units", [9, 10])
def test_no_overage_line_within_allowance(billing, store, units):
    store.get_plan("basic").included_units = 10
    billing.record_usage("s1", "event", units, date(2026, 1, 1))

    assert charges(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))

    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


def test_multiple_changes_usage_boundaries_and_late_events(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 5
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=10))
    store.add_plan(Plan("max", "Max", 9000, included_units=30, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))
    billing.record_usage("s1", "basic", 3, date(2026, 1, 10))
    billing.record_usage("s1", "pro", 9, date(2026, 1, 11))
    billing.record_usage("s1", "max", 11, date(2026, 1, 21))
    billing.record_usage("s1", "next", 40, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")

    assert charges(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 30),
        ("overage", "Overage: Max", 20),
    ]
    assert invoice.total_cents == 6060
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert store.get_invoice(invoice.invoice_id) is invoice
    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Max", 9000),
        ("overage", "Overage: Max", 200),
    ]


def test_segment_prices_round_half_up_independently(billing, store):
    store.get_plan("basic").monthly_price_cents = 3001
    store.add_plan(Plan("pro", "Pro", 6001))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1501),
        ("plan", "Plan: Pro", 3001),
    ]


def test_allowances_are_not_transferred_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=10, overage_unit_price_cents=7))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 6, date(2026, 1, 16))

    assert charges(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Pro", 7),
    ]


@pytest.mark.parametrize(
    "plan_id,effective_on,error",
    [
        ("pro", date(2026, 1, 1), ValueError),
        ("pro", date(2025, 12, 31), ValueError),
        ("pro", date(2026, 1, 31), ValueError),
        ("pro", date(2026, 2, 1), ValueError),
        ("basic", date(2026, 1, 2), ValueError),
        ("missing", date(2026, 1, 2), KeyError),
    ],
)
def test_rejected_initial_changes_leave_state_unchanged(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.__dict__)

    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.__dict__ == before


@pytest.mark.parametrize(
    "plan_id,effective_on",
    [
        ("basic", date(2026, 1, 10)),
        ("basic", date(2026, 1, 11)),
        ("pro", date(2026, 1, 12)),
    ],
)
def test_rejected_subsequent_changes_leave_state_unchanged(billing, store, plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.__dict__)

    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.__dict__ == before


def test_can_return_to_original_plan_and_change_again_next_period(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    assert billing.generate_invoice("s1").total_cents == 4000
    assert store.get_subscription("s1").plan_id == "basic"
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    assert billing.generate_invoice("s1").total_cents == 5000


def test_usage_subtotal_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "basic", 2, date(2026, 1, 15))
    billing.record_usage("s1", "pro", 3, date(2026, 1, 16))

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


def test_credit_is_capped_after_usage_discount(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=200))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 5, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1", "FIXED")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -200),
        ("credit", -3300),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1700


def test_usage_and_changes_survive_service_recreation(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))

    recreated = BillingService(store)

    assert recreated.record_usage("s1", "event", 3, date(2026, 1, 17)) is False
    assert recreated.generate_invoice("s1").total_cents == 4520


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    before = deepcopy(store.__dict__)

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert store.__dict__ == before
    assert billing.generate_invoice("s1").total_cents == 4520
