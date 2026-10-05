from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [
        (item.kind, item.description, item.amount_cents)
        for item in invoice.line_items
    ]


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_aggregated_before_included_units_are_subtracted(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    assert billing.record_usage("s1", "a", 7, date(2026, 1, 1))
    assert billing.record_usage("s1", "b", 8, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 125),
    ]
    assert invoice.total_cents == 3125
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_duplicate_usage_is_ignored_even_if_invalid_and_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "a", 3, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "a", 0, date(2026, 2, 5)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "a", -1, date(2025, 12, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_ids_are_scoped_to_each_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 2)) is True


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(ValueError):
        billing.record_usage("s1", "invalid", units, date(2026, 1, 3))

    assert subscription == before
    assert billing.record_usage("s1", "invalid", 1, date(2026, 1, 3)) is True


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "a", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert subscription == before


def test_future_and_late_events_bill_on_first_eligible_invoice(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 1, date(2025, 12, 1))
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))

    assert billing.generate_invoice("s1").total_cents == 3010
    billing.record_usage("s1", "late-arrival", 3, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_attribute_boundaries_and_late_usage(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=20))
    store.add_plan(Plan("max", "Max", 9000, included_units=30, overage_unit_price_cents=30))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))
    billing.record_usage("s1", "first", 3, date(2026, 1, 10))
    billing.record_usage("s1", "second", 8, date(2026, 1, 11))
    billing.record_usage("s1", "third", 13, date(2026, 1, 21))
    billing.record_usage("s1", "future", 31, date(2026, 1, 31))

    invoice = BillingService(store).generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 40),
        ("overage", "Overage: Max", 90),
    ]
    assert invoice.total_cents == 6150
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Max", 9000),
        ("overage", "Overage: Max", 30),
    ]


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)])
def test_changes_outside_open_period_leave_state_unchanged(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)

    assert subscription == before


def test_changes_must_be_strictly_ordered_and_change_current_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert subscription.plan_id == "pro"
    before = deepcopy(subscription)
    for plan_id, effective_on in [
        ("basic", date(2026, 1, 15)),
        ("basic", date(2026, 1, 16)),
        ("pro", date(2026, 1, 17)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
        assert subscription == before
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 1000),
        ("plan", "Plan: Basic", 1000),
    ]
    billing.change_plan("s1", "pro", date(2026, 2, 1))


def test_segment_prices_round_half_up_and_included_units_floor(billing, store):
    basic = store.get_plan("basic")
    basic.monthly_price_cents = 1001
    basic.included_units = 3
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 2001, included_units=3, overage_unit_price_cents=11))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 2, date(2026, 1, 15))
    billing.record_usage("s1", "second", 2, date(2026, 1, 16))

    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 501),
        ("plan", "Plan: Pro", 1001),
        ("overage", "Overage: Basic", 7),
        ("overage", "Overage: Pro", 11),
    ]


def test_usage_discount_credit_and_tax_order(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 25
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=50))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 4, date(2026, 1, 1))
    billing.record_usage("s1", "b", 4, date(2026, 1, 16))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000),
        ("overage", 100), ("overage", 200),
        ("discount", -480), ("credit", -500), ("tax", 382),
    ]
    assert invoice.total_cents == 4202
    assert customer.credit_balance_cents == 0


def test_credit_and_fixed_discount_are_capped_against_usage_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "a", 10, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=3500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 1000
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "FIXED")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -3500), ("credit", -500),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 500


def test_unused_allowances_do_not_transfer_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=10, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 6, date(2026, 1, 16))

    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Pro", 10),
    ]


def test_zero_price_overage_line_is_kept(billing):
    billing.record_usage("s1", "a", 1, date(2026, 1, 1))

    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


def test_rejected_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4510
