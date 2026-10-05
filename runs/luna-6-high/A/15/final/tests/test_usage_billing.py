from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


def test_usage_is_idempotent_and_invalid_units_are_rejected(billing):
    assert billing.record_usage("s1", "evt", 2, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "evt", 0, date(2026, 1, 20)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "other", 0, date(2026, 1, 5))


def test_usage_timing_late_events_and_overage(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 2
    basic.overage_unit_price_cents = 125

    billing.record_usage("s1", "late", 2, date(2025, 12, 30))
    billing.record_usage("s1", "inside", 3, date(2026, 1, 12))
    billing.record_usage("s1", "future", 4, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 375),
    ]
    assert "future" in store.get_subscription("s1").usage_events

    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 250),
    ]


def test_mid_period_plan_changes_prorate_and_allocate_usage(billing, store):
    store.add_plan(
        Plan(
            "pro",
            "Pro",
            6001,
            included_units=4,
            overage_unit_price_cents=250,
        )
    )
    basic = store.get_plan("basic")
    basic.included_units = 2
    basic.overage_unit_price_cents = 100

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "basic-usage", 3, date(2026, 1, 10))
    billing.record_usage("s1", "pro-usage", 6, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4001),
        ("overage", "Overage: Basic", 300),
        ("overage", "Overage: Pro", 1000),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_multiple_plan_changes_validate_without_mutating(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    store.add_plan(Plan("enterprise", "Enterprise", 9000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")

    with pytest.raises(ValueError):
        billing.change_plan("s1", "enterprise", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "enterprise", date(2026, 1, 31))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert [(change.plan_id, change.effective_on) for change in subscription.plan_changes] == [
        ("pro", date(2026, 1, 10))
    ]


def test_discount_credit_tax_apply_to_plan_and_overage_subtotal(billing, store):
    store.get_plan("basic").included_units = 0
    store.get_plan("basic").overage_unit_price_cents = 100
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    billing.record_usage("s1", "usage", 5, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
