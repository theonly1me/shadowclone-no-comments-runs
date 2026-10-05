from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def add_usage_plans(store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=10,
                        overage_unit_price_cents=100))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20,
                        overage_unit_price_cents=200))


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert subscription == before


def test_unknown_subscriptions(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))


def test_duplicates_are_ignored_even_with_invalid_payload_and_after_billing(billing, store):
    add_usage_plans(store)
    assert billing.record_usage("s1", "event", 11, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "event", 0, date(2030, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3100
    service = BillingService(store)
    assert service.record_usage("s1", "event", -1, date(2026, 2, 1)) is False
    assert service.generate_invoice("s1").total_cents == 3000


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1),
                                        date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 1)) is True
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 1)) is True


def test_future_events_wait_until_period_end_is_strictly_later(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "later-end", 3, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010
    assert billing.generate_invoice("s1").total_cents == 3020
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_late_event_is_billed_in_current_period_first_segment(billing, store):
    add_usage_plans(store)
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 15))
    billing.record_usage("s1", "late", 7, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 200),
    ]


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1),
                                         date(2026, 1, 31), date(2026, 2, 1)])
def test_change_must_be_strictly_inside_period(billing, store, effective_on):
    add_usage_plans(store)
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("plan_id,effective_on,error", [
    ("basic", date(2026, 1, 5), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("pro", date(2026, 1, 20), ValueError),
    ("missing", date(2026, 1, 20), KeyError),
])
def test_rejected_changes_do_not_modify_existing_changes(
    billing, store, plan_id, effective_on, error
):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_change_to_initial_current_plan_is_rejected(billing, store):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_multiple_changes_attribute_boundary_events_and_floor_allowances(billing, store):
    add_usage_plans(store)
    store.add_plan(Plan("max", "Max", 9000, included_units=30,
                        overage_unit_price_cents=300))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    for event_id, units, day in [("a", 2, 1), ("b", 3, 10),
                                 ("c", 8, 11), ("d", 11, 21)]:
        billing.record_usage("s1", event_id, units, date(2026, 1, day))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 200),
        ("overage", "Overage: Pro", 400),
        ("overage", "Overage: Max", 300),
    ]
    assert invoice.total_cents == 6900
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert (invoice.period_start, invoice.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert (subscription.period_start, subscription.period_end) == (date(2026, 1, 31), date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 9000


def test_can_change_back_and_start_a_new_change_sequence_next_period(billing, store):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert billing.generate_invoice("s1").total_cents == 4000
    billing.change_plan("s1", "pro", date(2026, 2, 1))
    assert billing.generate_invoice("s1").total_cents == 5900


def test_each_segment_rounds_half_up_independently(billing, store):
    store.get_subscription("s1").period_end = date(2026, 1, 3)
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2)
    ]


@pytest.mark.parametrize("units", [9, 10])
def test_usage_within_allowance_has_no_overage_line(billing, store, units):
    add_usage_plans(store)
    billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_zero_price_overage_is_still_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0)
    ]


def test_discount_credit_and_tax_apply_to_plan_and_overage_subtotal(billing, store):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "first", 7, date(2026, 1, 1))
    billing.record_usage("s1", "second", 12, date(2026, 1, 16))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500), ("plan", 3000), ("overage", 200), ("overage", 400),
        ("discount", -510), ("credit", -500), ("tax", 409),
    ]
    assert invoice.total_cents == 4499
    assert customer.credit_balance_cents == 0


def test_discount_and_credit_capped_at_usage_inclusive_subtotal(billing, store):
    add_usage_plans(store)
    billing.record_usage("s1", "event", 11, date(2026, 1, 1))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=1000))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 100), ("discount", -1000), ("credit", -2100)
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2900


def test_failed_invoice_does_not_consume_usage_or_plan_changes(billing, store):
    add_usage_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 7, date(2026, 1, 1))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4700
