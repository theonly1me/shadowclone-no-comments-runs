from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def amounts(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_invoicing_and_service_instances(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 5
    assert billing.record_usage("s1", "event", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3050
    another_service = BillingService(store)
    assert another_service.record_usage("s1", "event", -1, date(2026, 2, 5)) is False
    assert another_service.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1, -100])
def test_invalid_usage_does_not_reserve_event_id(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 1)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 5))


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 1))
    assert billing.record_usage("s2", "same", 1, date(2026, 1, 1))


def test_usage_boundaries_late_events_and_future_events(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "last", 2, date(2026, 1, 30))
    billing.record_usage("s1", "end", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 8, date(2026, 3, 2))
    billing.record_usage("s1", "late", 16, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3190
    billing.record_usage("s1", "arrived-late", 32, date(2026, 1, 10))
    assert billing.generate_invoice("s1").total_cents == 3360
    assert billing.generate_invoice("s1").total_cents == 3080
    assert billing.generate_invoice("s1").total_cents == 3000


def test_included_units_are_applied_to_aggregate_usage(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "a", 6, date(2026, 1, 1))
    billing.record_usage("s1", "b", 7, date(2026, 1, 2))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 21),
    ]


@pytest.mark.parametrize("units", [9, 10])
def test_usage_within_allowance_has_no_overage_line(billing, store, units):
    plan = store.get_plan("basic")
    plan.included_units = 10
    plan.overage_unit_price_cents = 7
    billing.record_usage("s1", "event", units, date(2026, 1, 1))
    assert amounts(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_multiple_changes_and_segment_usage(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 2
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=3))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "first", 3, date(2026, 1, 10))
    billing.record_usage("s1", "second", 8, date(2026, 1, 11))
    billing.record_usage("s1", "third", 7, date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert amounts(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 4),
        ("overage", "Overage: Pro", 6),
        ("overage", "Overage: Basic", 8),
    ]
    assert invoice.total_cents == 4018
    assert store.get_invoice(invoice.invoice_id) is invoice
    assert store.get_subscription("s1").plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 3000


def test_half_up_proration_and_final_plan_carries_forward(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1), ("plan", "Plan: Pro", 2),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    # A new period resets the change-date constraint.
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert billing.generate_invoice("s1").total_cents == 1


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)])
def test_out_of_period_change_leaves_state_unchanged(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_id == "basic"
    assert store.get_subscription("s1").plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 3000


def test_rejected_changes_preserve_existing_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    subscription = store.get_subscription("s1")
    saved_changes = list(subscription.plan_changes)
    for plan_id, day, error in [
        ("basic", 16, ValueError), ("basic", 15, ValueError),
        ("pro", 17, ValueError), ("missing", 17, KeyError),
    ]:
        with pytest.raises(error):
            billing.change_plan("s1", plan_id, date(2026, 1, day))
        assert subscription.plan_changes == saved_changes
        assert subscription.plan_id == "basic"
    assert billing.generate_invoice("s1").total_cents == 4500


def test_same_initial_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_usage_subtotal_discount_credit_tax_and_credit_cap(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "usage", 10, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
    customer.credit_balance_cents = 5000
    billing.record_usage("s1", "next", 10, date(2026, 2, 1))
    invoice = billing.generate_invoice("s1", "TEN")
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 1400
    assert [item.kind for item in invoice.line_items] == ["plan", "overage", "discount", "credit"]


def test_zero_price_overage_line_is_still_present(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 1))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_advance_period(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 5
    billing.record_usage("s1", "event", 10, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1").period_start == date(2026, 1, 1)
    assert billing.generate_invoice("s1").total_cents == 3050
