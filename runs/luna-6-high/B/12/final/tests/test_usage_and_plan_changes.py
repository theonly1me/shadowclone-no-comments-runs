from datetime import date

import pytest

from ledger.models import Plan


def test_usage_events_are_idempotent_and_billed_once(billing, store):
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
    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 1, date(2026, 1, 11)) is False

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000)
    ]


def test_usage_outside_open_period_waits_and_late_usage_is_billed(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]
    billing.record_usage("s1", "late", 2, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 2))


def test_mid_period_plan_changes_prorate_and_apply_usage_to_segments(billing, store):
    store.add_plan(
        Plan(
            plan_id="plus",
            name="Plus",
            monthly_price_cents=6000,
            included_units=10,
            overage_unit_price_cents=20,
        )
    )
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "before", 1, date(2026, 1, 15))
    billing.record_usage("s1", "after", 13, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Plus", 160),
    ]
    assert store.get_subscription("s1").plan_id == "plus"
    assert store.get_plan_changes("s1") == []


def test_plan_change_validation_is_atomic_and_ordered(billing, store):
    store.add_plan(Plan(plan_id="plus", name="Plus", monthly_price_cents=6000))
    store.add_plan(Plan(plan_id="max", name="Max", monthly_price_cents=9000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    before = store.get_plan_changes("s1")

    with pytest.raises(ValueError):
        billing.change_plan("s1", "max", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert store.get_plan_changes("s1") == before
    billing.change_plan("s1", "max", date(2026, 1, 20))
    assert [change.plan_id for change in store.get_plan_changes("s1")] == ["plus", "max"]
