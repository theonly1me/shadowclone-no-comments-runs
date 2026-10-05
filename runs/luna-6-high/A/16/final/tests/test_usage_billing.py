from datetime import date
import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 999, date(2026, 1, 20)) is False
    assert billing.record_usage("s1", "evt-1", 0, date(2026, 1, 20)) is False

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000)
    ]


def test_usage_on_or_after_period_end_is_deferred(billing):
    billing.record_usage("s1", "future", 4, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_usage_and_plan_change_are_segmented_and_prorated(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=60,
            overage_unit_price_cents=200,
        )
    )
    basic = store.get_plan("basic")
    basic.included_units = 30
    basic.overage_unit_price_cents = 100

    billing.record_usage("s1", "early", 15, date(2026, 1, 5))
    billing.record_usage("s1", "late", 55, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 500),
        ("overage", "Overage: Pro", 3000),
    ]
    assert invoice.total_cents == 8500
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_late_usage_is_assigned_to_first_segment(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    basic = store.get_plan("basic")
    basic.included_units = 0
    basic.overage_unit_price_cents = 10
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 20),
    ]


def test_invalid_usage_and_plan_changes_leave_state_unchanged(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "bad", 1, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 5))
    assert store.get_plan_changes("s1") == []

    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    assert [(change.effective_on, change.plan_id) for change in store.get_plan_changes("s1")] == [
        (date(2026, 1, 10), "pro")
    ]
