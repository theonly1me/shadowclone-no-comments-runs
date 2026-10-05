from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_overage_is_billed_by_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=30,
        )
    )
    store.add_plan(
        Plan(
            plan_id="plus",
            name="Plus",
            monthly_price_cents=6000,
            included_units=30,
            overage_unit_price_cents=20,
        )
    )
    billing.change_plan("s1", "plus", date(2026, 1, 11))

    assert billing.record_usage("s1", "old", 7, date(2025, 12, 20)) is True
    assert billing.record_usage("s1", "old", 100, date(2026, 1, 20)) is False
    assert billing.record_usage("s1", "old", 0, date(2026, 1, 20)) is False
    assert billing.record_usage("s1", "basic-use", 8, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "plus-use", 25, date(2026, 1, 20)) is True

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Plus", 4000),
        ("overage", "Overage: Basic", 360),
        ("overage", "Overage: Plus", 100),
    ]
    assert invoice.total_cents == 5460
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert billing.record_usage("s1", "old", 7, date(2025, 12, 20)) is False


def test_future_usage_waits_for_a_later_period(billing):
    billing.record_usage("s1", "later", 4, date(2026, 2, 1))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000)
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]


def test_plan_changes_must_be_ordered_and_different(billing, store):
    store.add_plan(Plan(plan_id="plus", name="Plus", monthly_price_cents=6000))
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=9000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))

    billing.change_plan("s1", "pro", date(2026, 1, 20))
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 900),
        ("plan", 2000),
        ("plan", 3300),
    ]
    assert store.get_subscription("s1").plan_id == "pro"


def test_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "invalid", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "unknown", date(2026, 1, 5))
