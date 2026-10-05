from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def amounts(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_idempotency_survives_invoicing_and_new_service(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 4, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 2, 5)) is False
    invoice = BillingService(store).generate_invoice("s1")
    assert invoice.total_cents == 3040
    assert billing.record_usage("s1", "event", -1, date(2025, 1, 5)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_record_event(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 5))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 5)) is True


def test_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 5))


def test_late_and_future_usage_are_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 3, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010
    billing.record_usage("s1", "late", 4, date(2025, 12, 20))
    assert billing.generate_invoice("s1").total_cents == 3060
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_usage_boundaries_and_allowance_floor(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 7
    store.add_plan(Plan("pro", "Pro", 6000, included_units=11, overage_unit_price_cents=9))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 4, date(2025, 12, 31))
    billing.record_usage("s1", "first", 1, date(2026, 1, 10))
    billing.record_usage("s1", "second", 5, date(2026, 1, 11))
    billing.record_usage("s1", "third", 4, date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert amounts(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 14),
        ("overage", "Overage: Pro", 18),
        ("overage", "Overage: Basic", 7),
    ]
    assert invoice.total_cents == 4039
    assert store.get_invoice(invoice.invoice_id) is invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []


def test_proration_rounds_each_segment_half_up_and_retains_final_plan(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1),
        ("plan", "Plan: Pro", 2),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    billing.change_plan("s1", "basic", date(2026, 2, 1))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Pro", 0),
        ("plan", "Plan: Basic", 1),
    ]


@pytest.mark.parametrize(
    "plan_id,effective_on,error",
    [
        ("basic", date(2026, 1, 16), ValueError),
        ("pro", date(2026, 1, 1), ValueError),
        ("pro", date(2025, 12, 31), ValueError),
        ("pro", date(2026, 1, 31), ValueError),
        ("pro", date(2026, 2, 1), ValueError),
        ("missing", date(2026, 1, 16), KeyError),
    ],
)
def test_rejected_changes_leave_state_unchanged(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)
    assert subscription == before


@pytest.mark.parametrize("day", [10, 15, 16])
def test_changes_must_be_strictly_chronological(billing, store, day):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, day))
    assert store.get_subscription("s1") == before
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert store.get_subscription("s1") == before


def test_overage_included_in_discount_credit_and_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 10, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_zero_price_overage_is_still_a_line_and_unused_allowance_is_not_shared(billing, store):
    store.get_plan("basic").included_units = 10
    store.add_plan(Plan("pro", "Pro", 3000, included_units=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 6, date(2026, 1, 16))
    assert amounts(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 1500),
        ("overage", "Overage: Pro", 0),
    ]


def test_event_ids_are_scoped_to_subscription(billing, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "event", 1, date(2026, 1, 5)) is True
