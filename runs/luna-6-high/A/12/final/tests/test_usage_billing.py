from datetime import date
from decimal import Decimal

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False

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
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000)
    ]


def test_usage_after_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "later", 4, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [item.kind for item in first.line_items] == ["plan"]

    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]
    assert billing.record_usage("s1", "later", 99, date(2026, 2, 1)) is False


def test_mid_period_plan_change_prorates_and_splits_included_units(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6001,
            included_units=20,
            overage_unit_price_cents=30,
        )
    )
    store.get_plan("basic").included_units = 31
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "old-segment", 20, date(2026, 1, 15))
    billing.record_usage("s1", "new-segment", 25, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3001),
        ("overage", "Overage: Basic", 50),
        ("overage", "Overage: Pro", 450),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_plan_change_validation_is_atomic(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    assert store.get_plan_changes("s1") == []


def test_multiple_plan_changes_are_chronological_and_last_plan_carries_forward(
    billing, store
):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    store.add_plan(Plan(plan_id="ultra", name="Ultra", monthly_price_cents=9000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "ultra", date(2026, 1, 21))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Basic", 1000),
        ("Plan: Pro", 2000),
        ("Plan: Ultra", 3000),
    ]
    assert store.get_subscription("s1").plan_id == "ultra"


def test_usage_validation_and_late_events(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "unknown-sub", 1, date(2026, 1, 10))

    store.get_plan("basic").included_units = 1
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [3000, 200]


def test_discount_credit_tax_apply_to_plan_and_overage_subtotal(billing, store):
    store.get_plan("basic").included_units = 0
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "metered", 2, date(2026, 1, 10))
    store.get_customer("c1").tax_rate_percent = Decimal("10")
    store.get_customer("c1").credit_balance_cents = 100

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("credit", -100),
        ("tax", 310),
    ]
