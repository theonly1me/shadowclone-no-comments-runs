from datetime import date
import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_only_once(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 2
    basic.overage_unit_price_cents = 25

    assert billing.record_usage("s1", "evt-1", 5, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False
    first = billing.generate_invoice("s1")
    assert [(line.kind, line.amount_cents) for line in first.line_items] == [
        ("plan", 3000),
        ("overage", 75),
    ]

    second = billing.generate_invoice("s1")
    assert [line.kind for line in second.line_items] == ["plan"]


def test_future_period_usage_waits_and_late_usage_is_billed(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 0
    basic.overage_unit_price_cents = 10

    billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [line.kind for line in first.line_items] == ["plan"]

    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    second = billing.generate_invoice("s1")
    assert [(line.kind, line.amount_cents) for line in second.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]


def test_mid_period_changes_split_plan_price_and_usage(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 30
    basic.overage_unit_price_cents = 10
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=30,
            overage_unit_price_cents=20,
        )
    )
    store.add_plan(
        Plan(
            plan_id="team",
            name="Team",
            monthly_price_cents=9000,
            included_units=60,
            overage_unit_price_cents=30,
        )
    )

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.record_usage("s1", "old-segment", 20, date(2026, 1, 5))
    billing.record_usage("s1", "middle-segment", 25, date(2026, 1, 15))
    billing.record_usage("s1", "last-segment", 65, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")
    assert [(line.kind, line.description, line.amount_cents) for line in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 3000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 300),
        ("overage", "Overage: Team", 1350),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "team"
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_change_validation_does_not_record_rejected_changes(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=5000))

    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 1))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")
    assert [line.description for line in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
    ]


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 1))
