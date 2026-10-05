from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_validation_and_idempotency(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "e", 1, date(2026, 1, 1))
    for units in (0, -1):
        with pytest.raises(ValueError):
            billing.record_usage("s1", "e", units, date(2026, 1, 1))
    assert billing.record_usage("s1", "e", 7, date(2026, 1, 1)) is True
    assert billing.record_usage("s1", "e", 0, date(2030, 1, 1)) is False
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s2", "e", 2, date(2026, 1, 1)) is True
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.generate_invoice("s1").total_cents == 3070
    assert billing.record_usage("s1", "e", 99, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_late_future_and_period_boundaries(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 10
    billing.record_usage("s1", "start", 5, date(2026, 1, 1))
    billing.record_usage("s1", "end", 7, date(2026, 1, 31))
    billing.record_usage("s1", "future", 8, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3000
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    # Pending events and idempotency survive a new service instance.
    service = BillingService(store)
    assert service.generate_invoice("s1").total_cents == 3040
    assert service.generate_invoice("s1").total_cents == 3030
    assert service.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_prorate_and_attribute_usage(billing, store):
    store.add_plan(Plan("basic", "Basic", 3001, included_units=31, overage_unit_price_cents=2))
    store.add_plan(Plan("pro", "Pro", 6001, included_units=61, overage_unit_price_cents=3))
    store.add_plan(Plan("max", "Max", 9001, included_units=91, overage_unit_price_cents=4))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))
    billing.record_usage("s1", "first", 10, date(2026, 1, 10))
    billing.record_usage("s1", "second", 24, date(2026, 1, 11))
    billing.record_usage("s1", "third", 35, date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 4),
        ("overage", "Overage: Pro", 12),
        ("overage", "Overage: Max", 20),
    ]
    assert invoice.total_cents == 6036
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "max"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Max", 9001)]


def test_proration_rounds_exact_halves_up(billing, store):
    subscription = store.get_subscription("s1")
    subscription.period_end = date(2026, 1, 3)
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("other", "Other", 1))
    billing.change_plan("s1", "other", date(2026, 1, 2))
    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [1, 1]


@pytest.mark.parametrize("plan_id,effective_on,error", [
    ("pro", date(2026, 1, 1), ValueError),
    ("pro", date(2026, 1, 31), ValueError),
    ("pro", date(2025, 12, 31), ValueError),
    ("pro", date(2026, 2, 1), ValueError),
    ("basic", date(2026, 1, 10), ValueError),
    ("missing", date(2026, 1, 10), KeyError),
])
def test_rejected_changes_leave_state_unchanged(billing, store, plan_id, effective_on, error):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_change_order_current_plan_and_reset(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "pro", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    for plan_id, day in (("basic", 14), ("basic", 15), ("pro", 16)):
        before = deepcopy(store.get_subscription("s1"))
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, date(2026, 1, day))
        assert store.get_subscription("s1") == before
    billing.change_plan("s1", "basic", date(2026, 1, 20))
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 1))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_overage_is_included_in_discount_credit_tax(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "e", 10, date(2026, 1, 10))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -400),
        ("credit", -500), ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_credit_is_capped_after_usage_discount(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "e", 10, date(2026, 1, 10))
    store.add_discount_code(DiscountCode("OFF", amount_off_cents=1000))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    invoice = billing.generate_invoice("s1", "OFF")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 1000), ("discount", -1000), ("credit", -3000),
    ]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=1))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "e", 10, date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4510


def test_positive_overage_with_zero_price_still_has_line(billing):
    billing.record_usage("s1", "e", 1, date(2026, 1, 1))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]
