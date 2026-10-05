from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3100,
            included_units=10,
            overage_unit_price_cents=25,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 900, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3100),
        ("overage", 50),
    ]

    next_invoice = billing.generate_invoice("s1")
    assert [item.kind for item in next_invoice.line_items] == ["plan"]


def test_future_usage_waits_for_period_and_late_usage_bills_now(billing, store):
    subscription = store.get_subscription("s1")
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            overage_unit_price_cents=50,
        )
    )
    subscription.plan_id = "metered"
    billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [item.kind for item in first.line_items] == ["plan"]

    billing.record_usage("s1", "late", 2, date(2026, 1, 15))
    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 250),
    ]


def test_midperiod_plan_changes_prorate_and_attribute_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=10,
            overage_unit_price_cents=100,
        )
    )
    store.add_plan(
        Plan(
            plan_id="team",
            name="Team",
            monthly_price_cents=9000,
            included_units=20,
            overage_unit_price_cents=200,
        )
    )
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.record_usage("s1", "old", 8, date(2026, 1, 5))
    billing.record_usage("s1", "pro", 6, date(2026, 1, 15))
    billing.record_usage("s1", "team", 25, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 3000),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Pro", 300),
        ("overage", "Overage: Team", 3800),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "team"
    assert subscription.plan_changes == []


def test_invalid_usage_and_plan_changes_leave_state_unchanged(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "bad", 1, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_plan_changes_must_be_increasing_and_switch_to_a_different_plan(
    billing, store
):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=5000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 31))

    assert [(change.effective_on, change.plan_id) for change in store.get_subscription("s1").plan_changes] == [
        (date(2026, 1, 10), "pro")
    ]
