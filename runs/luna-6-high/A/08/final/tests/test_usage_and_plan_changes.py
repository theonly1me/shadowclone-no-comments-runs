from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 2, 1)) is False

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000)
    ]
    assert len(store.get_subscription("s1").usage_events) == 1


def test_future_usage_waits_and_late_usage_bills_in_open_period(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=2,
            overage_unit_price_cents=50,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "future", 4, date(2026, 2, 1))
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 150),
    ]
    assert store.get_subscription("s1").usage_events["future"].billed is False

    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 100),
    ]


def test_plan_changes_prorate_and_usage_belongs_to_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="premium",
            name="Premium",
            monthly_price_cents=6001,
            included_units=20,
            overage_unit_price_cents=200,
        )
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "old-segment", 6, date(2026, 1, 15))
    billing.record_usage("s1", "new-segment", 12, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Premium", 3001),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Premium", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_rejected_plan_changes_leave_state_unchanged(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 31))
    assert store.get_subscription("s1").plan_changes == []


def test_usage_requires_positive_units_and_known_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "bad", 1, date(2026, 1, 1))
