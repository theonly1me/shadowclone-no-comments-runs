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

    assert billing.record_usage("s1", "evt-1", 15, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 125),
    ]
    assert billing.generate_invoice("s1").total_cents == 3000


def test_future_usage_waits_for_an_invoice_with_later_period_end(billing):
    billing.record_usage("s1", "future", 2, date(2026, 1, 31))

    assert billing.generate_invoice("s1").total_cents == 3000
    assert billing.generate_invoice("s1").total_cents == 3000


def test_change_plan_splits_price_and_usage_by_segment(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 1
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=10,
        )
    )
    billing.record_usage("s1", "old", 12, date(2026, 1, 10))
    billing.record_usage("s1", "new", 25, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 7),
        ("overage", "Overage: Pro", 150),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_late_usage_is_charged_in_first_segment_with_adjustments(billing, store):
    store.get_customer("c1").tax_rate_percent = Decimal("10")
    store.get_customer("c1").credit_balance_cents = 100
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=100,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "late", 2, date(2025, 12, 10))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("credit", -100),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410


def test_rejected_plan_changes_leave_existing_changes_unchanged(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    changes = list(subscription.plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert subscription.plan_changes == changes
