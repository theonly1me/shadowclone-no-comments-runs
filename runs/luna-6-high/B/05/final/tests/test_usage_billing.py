from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=5,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 7, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 20),
    ]
    assert billing.generate_invoice("s1").line_items == [
        invoice.line_items[0]
    ]


def test_plan_change_prorates_segments_and_attributes_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="premium",
            name="Premium",
            monthly_price_cents=6000,
            included_units=10,
            overage_unit_price_cents=30,
        )
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 20
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "early", 12, date(2026, 1, 10))
    billing.record_usage("s1", "late", 12, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Premium", 3000),
        ("overage", "Overage: Basic", 140),
        ("overage", "Overage: Premium", 210),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []


def test_future_and_late_usage_boundaries(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 0
    plan.overage_unit_price_cents = 10
    billing.record_usage("s1", "future", 4, date(2026, 1, 31))
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))

    first = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 20),
    ]
    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 40),
    ]


def test_rejected_plan_change_leaves_state_unchanged(billing, store):
    store.add_plan(Plan(plan_id="premium", name="Premium", monthly_price_cents=6000))
    billing.change_plan("s1", "premium", date(2026, 1, 10))
    changes = list(store.get_subscription("s1").plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", date(2026, 1, 9))

    assert store.get_subscription("s1").plan_changes == changes


def test_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 10))
