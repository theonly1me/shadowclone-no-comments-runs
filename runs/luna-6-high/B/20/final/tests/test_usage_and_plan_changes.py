from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_invalid_units_are_rejected(billing):
    assert billing.record_usage("s1", "evt-1", 4, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 20)) is False
    assert billing.record_usage("s1", "evt-1", 0, date(2026, 1, 20)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-2", 0, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt-3", 1, date(2026, 1, 10))


def test_usage_billing_boundaries_and_late_usage(billing, store):
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
    billing.record_usage("s1", "old", 3, date(2025, 12, 20))
    billing.record_usage("s1", "inside", 12, date(2026, 1, 10))
    billing.record_usage("s1", "boundary", 8, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 125),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
    ]


def test_plan_changes_prorate_segments_and_allocate_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6200,
            included_units=30,
            overage_unit_price_cents=50,
        )
    )
    store.add_plan(
        Plan(
            plan_id="team",
            name="Team",
            monthly_price_cents=3100,
            included_units=15,
            overage_unit_price_cents=100,
        )
    )
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.record_usage("s1", "a", 12, date(2026, 1, 5))
    billing.record_usage("s1", "b", 40, date(2026, 1, 15))
    billing.record_usage("s1", "c", 20, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2067),
        ("plan", "Plan: Team", 1033),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Pro", 1500),
        ("overage", "Overage: Team", 1500),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "team"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_invalid_plan_changes_leave_state_unchanged(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=5000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    changes = store.get_plan_changes("s1")

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))

    assert store.get_plan_changes("s1") == changes
