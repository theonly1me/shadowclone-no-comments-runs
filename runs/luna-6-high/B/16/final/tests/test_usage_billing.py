from datetime import date

import pytest

from ledger.models import Plan


def test_usage_events_are_idempotent_and_invalid_units_are_rejected(billing):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "evt-1", 9, date(2026, 1, 20)) is False
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-2", 0, date(2026, 1, 5))


def test_usage_is_billed_once_and_future_usage_waits_for_later_period(billing, store):
    billing.record_usage("s1", "late", 2, date(2025, 12, 20))
    billing.record_usage("s1", "inside", 3, date(2026, 1, 15))
    billing.record_usage("s1", "future", 4, date(2026, 2, 1))
    store.add_plan(
        Plan(
            plan_id="usage",
            name="Usage",
            monthly_price_cents=3000,
            included_units=4,
            overage_unit_price_cents=100,
        )
    )
    store.get_subscription("s1").plan_id = "usage"

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 100),
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000)
    ]


def test_plan_changes_prorate_and_attribute_usage_to_segments(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6001,
            included_units=10,
            overage_unit_price_cents=50,
        )
    )
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 25
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "basic-use", 6, date(2026, 1, 10))
    billing.record_usage("s1", "pro-use", 12, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3001),
        ("overage", "Overage: Basic", 25),
        ("overage", "Overage: Pro", 350),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_several_plan_changes_are_ordered_and_bad_changes_do_not_mutate(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    store.add_plan(Plan(plan_id="team", name="Team", monthly_price_cents=9000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 31))
    billing.change_plan("s1", "team", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Basic", 900),
        ("Plan: Pro", 2000),
        ("Plan: Team", 3300),
    ]
