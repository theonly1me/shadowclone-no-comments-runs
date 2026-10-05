from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_rejects_nonpositive_units(billing):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "evt-1", 8, date(2026, 1, 9)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-2", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt-3", 1, date(2026, 1, 5))


def test_usage_bills_only_when_event_is_before_period_end(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=2,
            overage_unit_price_cents=125,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "late", 2, date(2025, 12, 20))
    billing.record_usage("s1", "current", 3, date(2026, 1, 30))
    billing.record_usage("s1", "future", 10, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 375),
    ]
    assert [event.event_id for event in store.get_usage_events("s1")] == ["future"]
    assert billing.record_usage("s1", "current", 1, date(2026, 2, 1)) is False


def test_midperiod_changes_prorate_plans_and_attribute_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=2,
            overage_unit_price_cents=100,
        )
    )
    store.add_plan(
        Plan(
            plan_id="plus",
            name="Plus",
            monthly_price_cents=6000,
            included_units=9,
            overage_unit_price_cents=100,
        )
    )
    billing.change_plan("s1", "plus", date(2026, 1, 11))
    billing.record_usage("s1", "first", 4, date(2026, 1, 5))
    billing.record_usage("s1", "second", 5, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Plus", 4000),
        ("overage", "Overage: Basic", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_plan_changes_must_be_valid_and_ordered(billing, store):
    store.add_plan(Plan(plan_id="plus", name="Plus", monthly_price_cents=6000))
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=9000))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", date(2026, 1, 15))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 10))

    billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert [(change.plan_id, change.effective_on) for change in store.get_plan_changes("s1")] == [
        ("plus", date(2026, 1, 10)),
        ("pro", date(2026, 1, 20)),
    ]
