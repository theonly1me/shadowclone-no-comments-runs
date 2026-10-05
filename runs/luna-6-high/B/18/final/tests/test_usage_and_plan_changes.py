from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_invalid_units_are_rejected(billing):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", -5, date(2027, 1, 1)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-2", 0, date(2026, 1, 10))


def test_unknown_subscription_and_plan_are_rejected(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))


def test_segment_proration_usage_overage_and_rollover(billing, store):
    store.add_plan(
        Plan(
            "plus",
            "Plus",
            6000,
            included_units=200,
            overage_unit_price_cents=10,
        )
    )
    basic = store.get_plan("basic")
    basic.included_units = 100
    basic.overage_unit_price_cents = 5

    billing.record_usage("s1", "late", 60, date(2025, 12, 20))
    billing.record_usage("s1", "first", 10, date(2026, 1, 10))
    billing.record_usage("s1", "second", 120, date(2026, 1, 20))
    billing.record_usage("s1", "future", 1000, date(2026, 1, 31))
    billing.change_plan("s1", "plus", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Plus", 200),
    ]
    assert invoice.total_cents == 4800
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 6000),
        ("overage", 8000),
    ]


def test_plan_change_validation_does_not_mutate(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    state_before = list(store.get_subscription("s1").plan_changes)

    for effective_on in [date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 31)]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", "basic", effective_on)

    assert store.get_subscription("s1").plan_changes == state_before


def test_multiple_changes_create_chronological_plan_lines(billing, store):
    store.add_plan(Plan("plus", "Plus", 6000))
    store.add_plan(Plan("pro", "Pro", 9000))
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Basic", 1000),
        ("Plan: Plus", 2000),
        ("Plan: Pro", 3000),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
