from datetime import date

import pytest

from ledger.models import Plan


def _add_plans(store):
    store.add_plan(
        Plan(
            plan_id="premium",
            name="Premium",
            monthly_price_cents=6000,
            included_units=60,
            overage_unit_price_cents=20,
        )
    )


def test_usage_and_plan_changes_are_prorated_and_attributed_by_date(billing, store):
    _add_plans(store)
    store.get_plan("basic").included_units = 30
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.change_plan("s1", "premium", date(2026, 1, 11))
    assert billing.record_usage("s1", "early", 15, date(2026, 1, 5))
    assert billing.record_usage("s1", "late", 50, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Premium", 4000),
        ("overage", "Overage: Basic", 50),
        ("overage", "Overage: Premium", 200),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []


def test_usage_events_are_idempotent_and_future_events_defer(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 2
    assert billing.record_usage("s1", "evt", 3, date(2026, 1, 31)) is True
    assert billing.record_usage("s1", "evt", 0, date(2026, 1, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 6),
    ]


def test_late_usage_is_billed_once_on_current_open_period(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 5
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 10),
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
    ]


def test_invalid_plan_change_leaves_existing_changes_unchanged(billing, store):
    _add_plans(store)
    billing.change_plan("s1", "premium", date(2026, 1, 11))
    changes = list(store.get_subscription("s1").plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    assert store.get_subscription("s1").plan_changes == changes


def test_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))
