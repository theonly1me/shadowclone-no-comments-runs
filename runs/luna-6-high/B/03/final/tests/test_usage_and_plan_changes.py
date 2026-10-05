from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_only_when_period_is_over(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=100))
    assert billing.record_usage("s1", "event-1", 5, date(2026, 1, 31)) is True
    assert billing.record_usage("s1", "event-1", 10, date(2026, 1, 1)) is False

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000)
    ]

    billing.record_usage("s1", "event-2", 5, date(2026, 2, 1))
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000)
    ]
    assert store.get_subscription("s1").usage_events["event-1"].billed


def test_late_usage_is_billed_in_first_segment(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=2, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("metered", "Metered", 3100, included_units=4, overage_unit_price_cents=25)
    )
    billing.change_plan("s1", "metered", date(2026, 1, 16))
    billing.record_usage("s1", "late", 7, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Metered", 1550),
        ("overage", "Overage: Basic", 60),
    ]


def test_usage_and_overages_are_segmented(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, overage_unit_price_cents=25)
    )
    store.add_plan(
        Plan("metered", "Metered", 3100, included_units=2, overage_unit_price_cents=25)
    )
    billing.change_plan("s1", "metered", date(2026, 1, 16))
    billing.record_usage("s1", "first", 5, date(2026, 1, 15))
    billing.record_usage("s1", "second", 7, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 1550),
        ("overage", 125),
        ("overage", 150),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []


def test_usage_units_must_be_positive_and_subscription_must_exist(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "invalid", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "invalid", 1, date(2026, 1, 2))


def test_plan_change_validation_leaves_state_unchanged(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_plan_change_dates_are_strict_and_chronological(billing, store):
    store.add_plan(Plan("plus", "Plus", 4500))
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))
    billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert [change.plan_id for change in store.get_subscription("s1").plan_changes] == [
        "plus",
        "pro",
    ]
