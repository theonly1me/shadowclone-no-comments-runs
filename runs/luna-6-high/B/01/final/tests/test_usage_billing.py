from datetime import date

import pytest

from ledger.models import Plan


def test_usage_event_is_idempotent_and_invalid_units_are_rejected(billing):
    assert billing.record_usage("s1", "e1", 3, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "e1", 99, date(2030, 1, 1)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", 0, date(2026, 1, 10))


def test_future_usage_waits_and_late_usage_is_billed_in_open_period(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=2, overage_unit_price_cents=100))
    billing.record_usage("s1", "future", 8, date(2026, 1, 31))
    billing.record_usage("s1", "late", 5, date(2025, 12, 20))

    first = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 300),
    ]
    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 600),
    ]


def test_plan_changes_prorate_lines_and_attribute_usage_by_segment(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=10, overage_unit_price_cents=10))
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=100))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "old", 7, date(2026, 1, 15))
    billing.record_usage("s1", "new", 25, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 1500),
    ]
    assert invoice.total_cents == 6020
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_rejected_plan_change_does_not_modify_schedule(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert [(change.plan_id, change.effective_on) for change in store.get_plan_changes("s1")] == [
        ("pro", date(2026, 1, 10))
    ]
