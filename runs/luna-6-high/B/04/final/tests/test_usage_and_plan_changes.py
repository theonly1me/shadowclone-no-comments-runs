from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_only_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            included_units=10,
            overage_unit_price_cents=25,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("overage", 50),
    ]
    assert billing.generate_invoice("s1").line_items[0].kind == "plan"


def test_future_usage_waits_until_period_end_is_later(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            included_units=0,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "evt-future", 3, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 1000),
        ("overage", 30),
    ]


def test_plan_change_prorates_and_attributes_usage_by_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=1,
        )
    )
    store.add_plan(
        Plan(
            plan_id="plus",
            name="Plus",
            monthly_price_cents=6000,
            included_units=4,
            overage_unit_price_cents=20,
        )
    )
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "evt-old", 5, date(2026, 1, 10))
    billing.record_usage("s1", "evt-new", 6, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 5),
        ("overage", "Overage: Plus", 80),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []


def test_rejected_plan_change_leaves_changes_unchanged(billing, store):
    store.add_plan(Plan(plan_id="plus", name="Plus", monthly_price_cents=6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    existing_changes = list(subscription.plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "plus", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    assert subscription.plan_changes == existing_changes


def test_record_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.record_usage("unknown", "evt", 1, date(2026, 1, 10))
