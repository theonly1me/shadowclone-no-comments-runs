from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_invalid_units_are_rejected(billing):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "evt-1", 0, date(2026, 1, 6)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-2", 0, date(2026, 1, 6))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt-3", 1, date(2026, 1, 6))


def test_usage_is_billed_only_when_event_date_is_before_period_end(billing, store):
    store.add_plan(Plan("metered", "Metered", 3000, 2, 100))
    subscription = store.get_subscription("s1")
    subscription.plan_id = "metered"
    billing.record_usage("s1", "inside", 5, date(2026, 1, 30))
    billing.record_usage("s1", "boundary", 7, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 300),
    ]

    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
    ]


def test_late_usage_is_billed_in_open_period(billing, store):
    store.add_plan(Plan("metered", "Metered", 3000, 0, 25))
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "late", 4, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 100),
    ]


def test_plan_changes_prorate_and_split_usage_by_effective_date(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, 9, 250))
    store.add_plan(Plan("metered", "Metered", 6000, 30, 200))
    billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.record_usage("s1", "before", 5, date(2026, 1, 10))
    billing.record_usage("s1", "after", 25, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Metered", 4000),
        ("overage", "Overage: Basic", 500),
        ("overage", "Overage: Metered", 1000),
    ]
    assert invoice.total_cents == 6500
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert store.get_plan_changes("s1") == []


def test_rejected_plan_change_does_not_mutate_state(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert [(change.plan_id, change.effective_on) for change in store.get_plan_changes("s1")] == [
        ("pro", date(2026, 1, 10))
    ]
