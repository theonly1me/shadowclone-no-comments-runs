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

    assert billing.record_usage("s1", "event", 3, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 5, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", -5, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_no_event(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))

    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True


def test_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "event", 2, date(2026, 1, 2)) is True
    assert billing.generate_invoice("s1").total_cents == 3010
    assert billing.generate_invoice("s2").total_cents == 3020


def test_usage_boundaries_late_events_and_future_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    for event_id, units, occurred_on in [
        ("late", 1, date(2025, 12, 31)),
        ("start", 2, date(2026, 1, 1)),
        ("last", 3, date(2026, 1, 30)),
        ("end", 4, date(2026, 1, 31)),
        ("future", 5, date(2026, 3, 2)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    assert billing.generate_invoice("s1").total_cents == 3060
    billing.record_usage("s1", "arrived-late", 6, date(2026, 1, 5))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_allowance_is_aggregated_and_resets_each_period(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 20
    billing.record_usage("s1", "a", 3, date(2026, 1, 1))
    billing.record_usage("s1", "b", 4, date(2026, 1, 2))

    assert billing.generate_invoice("s1").total_cents == 3040
    billing.record_usage("s1", "c", 5, date(2026, 1, 31))
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]


def test_multiple_changes_prorate_and_continue_on_final_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    store.add_plan(Plan("premium", "Premium", 9000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "premium", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Premium", 3000),
    ]
    assert invoice.total_cents == 6000
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert billing.generate_invoice("s1").total_cents == 9000


def test_plan_proration_rounds_each_segment_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1, 2]
    assert invoice.total_cents == 3


def test_segment_usage_allowances_floor_and_event_attribution(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 5
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=7, overage_unit_price_cents=20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("first", 1, date(2026, 1, 10)),
        ("boundary", 4, date(2026, 1, 11)),
        ("last", 2, date(2026, 1, 30)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 40),
    ]


def test_unused_allowance_does_not_transfer_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=2, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 3, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000), ("overage", 20)
    ]


def test_returning_to_initial_plan_keeps_segments_separate(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 3
    basic.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "first", 3, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000), ("plan", 2000), ("plan", 1000), ("overage", 20)
    ]
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)],
)
def test_change_must_be_strictly_inside_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)

    assert store.get_subscription("s1") == before


@pytest.mark.parametrize(
    "new_plan_id,effective_on,error",
    [
        ("basic", date(2026, 1, 10), ValueError),
        ("basic", date(2026, 1, 11), ValueError),
        ("pro", date(2026, 1, 12), ValueError),
        ("missing", date(2026, 1, 12), KeyError),
    ],
)
def test_rejected_changes_leave_state_unchanged(
    billing, store, new_plan_id, effective_on, error
):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(error):
        billing.change_plan("s1", new_plan_id, effective_on)

    assert store.get_subscription("s1") == before
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000


def test_changing_to_initial_current_plan_is_rejected(billing, store):
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))

    assert store.get_subscription("s1") == before


def test_overage_is_included_before_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=200))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 1, date(2026, 1, 1))
    billing.record_usage("s1", "b", 2, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 3000),
        ("overage", 100),
        ("overage", 400),
        ("discount", -500),
        ("credit", -500),
        ("tax", 400),
    ]
    assert invoice.total_cents == 4400
    assert customer.credit_balance_cents == 0


def test_credit_is_capped_after_discount_on_usage_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=500))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "a", 5, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1", "FIXED")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 500), ("discount", -500), ("credit", -3000)
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000


def test_positive_overage_with_zero_price_still_has_line(billing):
    billing.record_usage("s1", "a", 1, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 0)
    ]


def test_usage_and_changes_persist_across_service_instances(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 1, date(2026, 1, 16))

    other = BillingService(store)

    assert other.record_usage("s1", "a", 2, date(2026, 1, 1)) is False
    assert other.generate_invoice("s1").total_cents == 4510
    assert billing.generate_invoice("s1").total_cents == 6000


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 1, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")

    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4510


def test_new_period_accepts_changes_with_fresh_ordering(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 30))
    billing.generate_invoice("s1")
    billing.change_plan("s1", "basic", date(2026, 2, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 200), ("plan", 2900)
    ]
