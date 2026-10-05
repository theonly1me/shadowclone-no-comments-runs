from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=3,
            overage_unit_price_cents=25,
        )
    )
    assert billing.record_usage("s1", "evt", 5, date(2026, 1, 20)) is True
    assert billing.record_usage("s1", "evt", 99, date(2026, 1, 2)) is False

    invoice = billing.generate_invoice("s1")
    assert [(line.kind, line.amount_cents) for line in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]
    assert [(line.kind, line.amount_cents) for line in billing.generate_invoice("s1").line_items] == [
        ("plan", 3000)
    ]


def test_usage_outside_period_waits_and_late_usage_is_current(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            overage_unit_price_cents=10,
        )
    )
    billing.record_usage("s1", "future", 2, date(2026, 2, 1))
    billing.record_usage("s1", "late", 3, date(2025, 12, 1))

    first = billing.generate_invoice("s1")
    assert [(line.kind, line.amount_cents) for line in first.line_items] == [
        ("plan", 3000),
        ("overage", 30),
    ]
    second = billing.generate_invoice("s1")
    assert [(line.kind, line.amount_cents) for line in second.line_items] == [
        ("plan", 3000),
        ("overage", 20),
    ]


def test_plan_changes_prorate_and_attribute_usage_by_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=100,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=200,
        )
    )
    billing.record_usage("s1", "basic-use", 5, date(2026, 1, 5))
    billing.record_usage("s1", "pro-use", 15, date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")
    assert [(line.kind, line.description, line.amount_cents) for line in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 200),
        ("overage", "Overage: Pro", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_rejected_plan_change_does_not_mutate_state(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=5000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = list(store.get_plan_changes("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    assert store.get_plan_changes("s1") == before


def test_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "ok", 1, date(2026, 1, 1))
