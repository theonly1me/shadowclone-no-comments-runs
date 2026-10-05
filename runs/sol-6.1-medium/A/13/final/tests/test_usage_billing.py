from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def items(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("flat", "Flat", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_for_invalid_retry_and_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 4, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2030, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscriptions_raise_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 2)) is True


def test_future_boundary_and_late_events_are_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010

    billing.record_usage("s1", "late", 4, date(2025, 12, 1))
    # Usage survives replacing the service, because the store owns the state.
    billing = BillingService(store)
    assert billing.generate_invoice("s1").total_cents == 3060
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_full_period_included_units_and_zero_price_overage(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 20
    billing.record_usage("s1", "one", 2, date(2026, 1, 1))
    billing.record_usage("s1", "two", 3, date(2026, 1, 30))
    assert items(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]

    plan.overage_unit_price_cents = 0
    billing.record_usage("s1", "three", 6, date(2026, 2, 1))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]


def test_unused_allowance_is_not_shared_between_segments(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 300
    store.add_plan(Plan("pro", "Pro", 6000, included_units=30, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "event", 21, date(2026, 1, 11))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Pro", 10),
    ]


@pytest.mark.parametrize("amount_off_cents", [1000, 5000])
def test_usage_credit_cap_and_zero_adjustments(billing, store, amount_off_cents):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=amount_off_cents))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "OFF")
    discount = min(amount_off_cents, 3100)
    credit = 3100 - discount
    expected = [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
        ("discount", "Discount", -discount),
    ]
    if credit:
        expected.append(("credit", "Account credit", -credit))
    assert items(invoice) == expected
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 5000 - credit


def test_multiple_changes_usage_boundaries_and_adjustments(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert store.get_subscription("s1").plan_id == "basic"

    # Three ten-day segments: allowances floor to 3, 6, and 3.
    for event_id, units, occurred_on in [
        ("late", 1, date(2025, 12, 31)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 8, date(2026, 1, 11)),
        ("third", 6, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "TEN")
    assert items(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 40),
        ("overage", "Overage: Basic", 30),
        ("discount", "Discount", -408),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 317),
    ]
    assert invoice.total_cents == 3489
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert (invoice.period_start, invoice.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    subscription = store.get_subscription("s1")
    assert subscription.plan_changes == []
    assert subscription.plan_id == "basic"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("tax", "Tax", 300)
    ]


def test_proration_rounds_half_up_and_next_period_uses_final_plan(billing, store):
    subscription = store.get_subscription("s1")
    subscription.period_end = date(2026, 1, 3)
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    assert subscription.plan_id == "pro"
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2)
    ]
    assert subscription.plan_id == "pro"
    assert items(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 3)]
    # Previous-period change dates impose no constraint in a new period.
    billing.change_plan("s1", "basic", date(2026, 1, 6))


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1),
                                       date(2026, 1, 31), date(2026, 2, 1)])
def test_changes_outside_open_period_are_atomic(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("basic", date(2026, 1, 2), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("pro", date(2026, 1, 11), ValueError),
    ("missing", date(2026, 1, 11), KeyError),
])
def test_rejected_changes_preserve_accepted_history(
    billing, store, new_plan_id, effective_on, error
):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_changing_to_initial_current_plan_is_rejected(billing, store):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_failed_invoice_does_not_consume_usage_credit_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "event", 10, date(2026, 1, 11))
    store.get_customer("c1").credit_balance_cents = 500
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert store.get_customer("c1").credit_balance_cents == 500
    assert billing.generate_invoice("s1").total_cents == 4700
