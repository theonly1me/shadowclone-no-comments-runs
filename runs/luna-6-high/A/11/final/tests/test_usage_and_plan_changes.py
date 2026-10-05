from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan("metered", "Metered", 3000, included_units=5, overage_unit_price_cents=20)
    )
    store.get_subscription("s1").plan_id = "metered"
    assert billing.record_usage("s1", "e1", 8, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 100, date(2026, 1, 31)) is False

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(x.kind, x.amount_cents) for x in first.line_items] == [
        ("plan", 3000),
        ("overage", 60),
    ]
    assert [(x.kind, x.amount_cents) for x in second.line_items] == [("plan", 3000)]


def test_future_usage_waits_and_late_usage_is_billed_next_open_period(billing, store):
    plan = store.get_plan("basic")
    plan.overage_unit_price_cents = 10
    assert billing.record_usage("s1", "future", 4, date(2026, 1, 31))
    assert billing.record_usage("s1", "late", 3, date(2025, 12, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(x.kind, x.amount_cents) for x in first.line_items] == [
        ("plan", 3000),
        ("overage", 30),
    ]
    assert [(x.kind, x.amount_cents) for x in second.line_items] == [
        ("plan", 3000),
        ("overage", 40),
    ]


def test_change_plan_splits_plan_and_usage_lines_chronologically(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000, included_units=20, overage_unit_price_cents=30))
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 20
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    assert billing.record_usage("s1", "old-segment", 8, date(2026, 1, 15))
    assert billing.record_usage("s1", "new-segment", 15, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(x.kind, x.description, x.amount_cents) for x in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 60),
        ("overage", "Overage: Plus", 150),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []


def test_rejected_plan_change_leaves_state_unchanged(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))

    assert subscription.plan_changes == before


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 5))
