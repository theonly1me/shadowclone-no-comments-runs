from datetime import date

import pytest

from ledger.models import Plan


def test_usage_events_are_idempotent_and_future_events_wait(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=4, overage_unit_price_cents=5)
    )
    assert billing.record_usage("s1", "e1", 4, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 20)) is False
    assert billing.record_usage("s1", "e2", 7, date(2026, 1, 31)) is True

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000)
    ]

    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 15),
    ]


def test_late_usage_is_billed_in_current_period(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=3, overage_unit_price_cents=25)
    )
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]


def test_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))


def test_mid_period_plan_changes_prorate_and_split_usage(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=10, overage_unit_price_cents=100)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=4, overage_unit_price_cents=200)
    )
    billing.record_usage("s1", "old", 8, date(2026, 1, 10))
    billing.record_usage("s1", "new", 8, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 300),
        ("overage", "Overage: Pro", 1200),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_plan_changes("s1") == []


def test_plan_change_validation_does_not_record_rejected_change(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    billing.change_plan("s1", "basic", date(2026, 1, 20))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 25))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 15))

    assert [(change.plan_id, change.effective_on) for change in store.get_plan_changes("s1")] == [
        ("pro", date(2026, 1, 10)),
        ("basic", date(2026, 1, 20)),
    ]


def test_unknown_plan_change_leaves_state_unchanged(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))

    assert store.get_plan_changes("s1") == []
