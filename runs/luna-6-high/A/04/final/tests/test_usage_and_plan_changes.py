from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=25,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 2, 5)) is False
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]

    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000)
    ]


def test_future_and_late_usage_events(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    billing.record_usage("s1", "late", 2, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 20),
    ]
    following = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in following.line_items] == [
        ("plan", 3000),
        ("overage", 30),
    ]


def test_plan_changes_prorate_segments_and_attribute_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=30,
        )
    )
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "before", 8, date(2026, 1, 5))
    billing.record_usage("s1", "after", 25, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Pro", 360),
    ]
    assert invoice.total_cents == 5360
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)


def test_invalid_usage_and_plan_changes_do_not_mutate(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "bad", 1, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "unknown", date(2026, 1, 10))

    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))
    assert len(store.get_plan_changes("s1")) == 1
