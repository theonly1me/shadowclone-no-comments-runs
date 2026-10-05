from datetime import date
import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=5,
            overage_unit_price_cents=20,
        )
    )
    subscription = store.get_subscription("s1")
    subscription.plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 8, date(2026, 1, 15)) is True
    assert billing.record_usage("s1", "evt-1", 900, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 60),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000)
    ]


def test_usage_before_open_period_is_billed_and_period_end_is_not(billing, store):
    plan = store.get_plan("basic")
    plan.included_units = 2
    plan.overage_unit_price_cents = 15
    billing.record_usage("s1", "late", 3, date(2025, 12, 20))
    billing.record_usage("s1", "boundary", 4, date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 15),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 30),
    ]


def test_mid_period_plan_change_prorates_plan_and_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=30,
            overage_unit_price_cents=7,
        )
    )
    basic = store.get_plan("basic")
    basic.included_units = 30
    basic.overage_unit_price_cents = 5
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "old", 14, date(2026, 1, 5))
    billing.record_usage("s1", "new", 22, date(2026, 1, 15))
    billing.record_usage("s1", "late", 1, date(2025, 12, 31))

    invoice = billing.generate_invoice("s1")

    assert [
        (item.kind, item.description, item.amount_cents) for item in invoice.line_items
    ] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 25),
        ("overage", "Overage: Pro", 14),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_plan_changes_are_validated_without_partial_mutation(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")
    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
    ]


def test_usage_and_plan_change_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 5))
