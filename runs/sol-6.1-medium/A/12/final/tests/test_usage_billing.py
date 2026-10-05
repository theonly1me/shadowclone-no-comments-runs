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
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_aggregated_before_applying_allowance(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 25
    assert billing.record_usage("s1", "a", 8, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "b", 7, date(2026, 1, 30)) is True

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 125),
    ]
    assert invoice.total_cents == 3125
    assert store.get_invoice(invoice.invoice_id) == invoice


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_new_usage_does_not_consume_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "a", units, date(2026, 1, 2))
    assert billing.record_usage("s1", "a", 1, date(2026, 1, 2)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "a", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_duplicates_are_ignored_completely_even_after_billing(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "a", 2, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "a", 0, date(2027, 1, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3020

    # State belongs to the store, not to one service instance.
    another_service = BillingService(store)
    assert another_service.record_usage("s1", "a", -1, date(2025, 1, 2)) is False
    assert lines(another_service.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000)
    ]


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription(
        "s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)
    ))
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 2)) is True
    assert billing.generate_invoice("s1").total_cents == 3010
    assert billing.generate_invoice("s2").total_cents == 3020


def test_future_and_late_events_are_billed_on_next_eligible_invoice(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 1, date(2025, 12, 1))
    billing.record_usage("s1", "boundary", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010

    billing.record_usage("s1", "new-late", 8, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_segment_usage_and_line_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20,
                        overage_unit_price_cents=11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    # Each ten-day segment has an independently floored allowance: 3, 6, 3.
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("start", 3, date(2026, 1, 1)),
        ("first-boundary", 8, date(2026, 1, 11)),
        ("second-boundary", 7, date(2026, 1, 21)),
        ("end", 100, date(2026, 1, 31)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 14),
        ("overage", "Overage: Pro", 22),
        ("overage", "Overage: Basic", 28),
    ]
    assert invoice.total_cents == 4064
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_subscription("s1").plan_changes == []


def test_proration_uses_half_up_rounding_for_each_segment(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1),
        ("plan", "Plan: Pro", 2),
    ]


def test_final_plan_carries_forward_and_new_period_can_have_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert billing.generate_invoice("s1").total_cents == 4500
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 6000)]
    billing.change_plan("s1", "basic", date(2026, 3, 17))
    assert billing.generate_invoice("s1").total_cents == 4500
    assert subscription.plan_id == "basic"


@pytest.mark.parametrize("new_plan_id,effective_on,error", [
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 15), ValueError),
    ("missing", date(2026, 1, 15), KeyError),
])
def test_rejected_first_change_leaves_state_unchanged(
    billing, store, new_plan_id, effective_on, error
):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 11)),
    ("pro", date(2026, 1, 12)),
])
def test_change_must_follow_previous_change_and_differ_from_current(
    billing, store, new_plan_id, effective_on
):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert subscription == before
    assert billing.generate_invoice("s1").total_cents == 5000


def test_discount_credit_and_tax_apply_to_plan_plus_usage(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 1, date(2026, 1, 2))
    billing.record_usage("s1", "b", 2, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000), ("overage", 100), ("overage", 400),
        ("discount", -500), ("credit", -500), ("tax", 400),
    ]
    assert invoice.total_cents == 4400
    assert customer.credit_balance_cents == 0


def test_allowance_does_not_transfer_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=10,
                        overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 6, date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Pro", 20),
    ]


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "a", 1, date(2026, 1, 2))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 2, date(2026, 1, 20))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4520
