from datetime import date
from decimal import Decimal

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=25,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 13, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 75),
    ]
    assert billing.record_usage("s1", "evt-1", 1, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").line_items[0].kind == "plan"


def test_future_usage_waits_for_period(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "evt-future", 4, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000)
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 40),
    ]


def test_late_usage_is_billed_in_open_period(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "evt-late", 2, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 20),
    ]


def test_plan_changes_prorate_and_split_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="premium",
            name="Premium",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=200,
        )
    )
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 100
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "evt-basic", 8, date(2026, 1, 5))
    billing.record_usage("s1", "evt-premium", 12, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Premium", 3000),
        ("overage", "Overage: Basic", 300),
        ("overage", "Overage: Premium", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_invalid_usage_and_plan_changes_leave_state_unchanged(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))

    assert store.get_subscription("s1").plan_changes == []
    assert store.get_subscription("s1").usage_events == []


def test_discount_credit_and_tax_follow_usage_subtotal(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "evt", 100, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("credit", -500),
        ("tax", 350),
    ]
    assert invoice.total_cents == 3850
    assert customer.credit_balance_cents == 0
