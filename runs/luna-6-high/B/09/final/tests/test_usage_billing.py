from datetime import date

import pytest

from ledger.models import Plan


def test_usage_event_is_idempotent_and_billed_once(billing, store):
    assert billing.record_usage("s1", "evt-1", 4, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]
    assert billing.generate_invoice("s1").total_cents == 3000


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-1", 0, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt-1", 1, date(2026, 1, 10))


def test_future_usage_waits_and_late_usage_bills_in_open_period(billing, store):
    billing.record_usage("s1", "future", 3, date(2026, 2, 1))
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    store.add_plan(
        Plan(
            plan_id="usage",
            name="Usage",
            monthly_price_cents=3000,
            included_units=1,
            overage_unit_price_cents=25,
        )
    )
    store.get_subscription("s1").plan_id = "usage"

    first_invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 25),
    ]
    assert billing.generate_invoice("s1").total_cents == 3050


def test_plan_changes_prorate_and_usage_is_attributed_by_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=100,
        )
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 50

    billing.record_usage("s1", "basic-use", 7, date(2026, 1, 10))
    billing.record_usage("s1", "pro-use", 12, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 200),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_changes_validate_without_mutating(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    prior_changes = list(subscription.plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert subscription.plan_changes == prior_changes


def test_plan_price_uses_half_up_proration(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.get_subscription("s1").period_end = date(2026, 1, 3)
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=1))

    billing.change_plan("s1", "pro", date(2026, 1, 2))

    assert [item.amount_cents for item in billing.generate_invoice("s1").line_items] == [
        1,
        1,
    ]
