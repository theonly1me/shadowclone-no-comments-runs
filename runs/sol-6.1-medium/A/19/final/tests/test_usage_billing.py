from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("p", "Plan", 100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_is_per_subscription_and_survives_billing(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 2)) is False
    assert billing.record_usage("s2", "event", 1, date(2026, 1, 2)) is True
    billing.generate_invoice("s1")
    assert BillingService(store).record_usage("s1", "event", -1, date(2026, 3, 2)) is False


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2))


def test_unknown_ids_raise_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))


def test_usage_cutoff_late_events_and_bill_only_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "start", 3, date(2026, 1, 1))
    billing.record_usage("s1", "end", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 5, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3050
    billing.record_usage("s1", "arrived-late", 6, date(2026, 1, 5))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_usage_boundaries_and_next_period(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=10, overage_unit_price_cents=7))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 1)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 8, date(2026, 1, 11)),
        ("third", 7, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    invoice = BillingService(store).generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 14),
        ("overage", "Overage: Pro", 22),
        ("overage", "Overage: Basic", 28),
    ]
    assert invoice.total_cents == 4064
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_end_plan_is_carried_forward_and_changes_restart_each_period(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert billing.generate_invoice("s1").total_cents == 4500
    assert store.get_subscription("s1").plan_id == "pro"
    billing.change_plan("s1", "basic", date(2026, 2, 15))
    assert billing.generate_invoice("s1").total_cents == 4500


@pytest.mark.parametrize("plan_id,effective_on,error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 2), ValueError),
    ("missing", date(2026, 1, 2), KeyError),
])
def test_rejected_first_change_is_atomic(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("plan_id,effective_on", [
    ("basic", date(2026, 1, 9)),
    ("basic", date(2026, 1, 10)),
    ("pro", date(2026, 1, 11)),
])
def test_rejected_later_change_preserves_history(billing, store, plan_id, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_plan_proration_rounds_each_segment_half_up(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2),
    ]


def test_usage_included_allowance_and_zero_price_overage(billing, store):
    store.get_plan("basic").included_units = 5
    billing.record_usage("s1", "included", 5, date(2026, 1, 2))
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]
    billing.record_usage("s1", "over", 6, date(2026, 2, 2))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_adjustments_apply_to_combined_plan_and_usage_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.get_customer("c1").credit_balance_cents = 500
    store.get_customer("c1").tax_rate_percent = Decimal("10")
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    billing.record_usage("s1", "event", 10, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert store.get_customer("c1").credit_balance_cents == 0


def test_discount_and_credit_capped_at_usage_inclusive_subtotal(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.get_customer("c1").credit_balance_cents = 5000
    store.get_customer("c1").tax_rate_percent = Decimal("10")
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=500))
    billing.record_usage("s1", "event", 10, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "FIXED")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -500), ("credit", -3500),
    ]
    assert invoice.total_cents == 0
    assert store.get_customer("c1").credit_balance_cents == 1500
