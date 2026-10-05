from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=4,
            overage_unit_price_cents=125,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 7, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 375),
    ]
    assert billing.generate_invoice("s1").total_cents == 3000


def test_future_usage_waits_and_late_usage_bills_current_period(billing, store):
    store.get_subscription("s1").period_start = date(2026, 2, 1)
    store.get_subscription("s1").period_end = date(2026, 3, 1)
    store.get_plan("basic").included_units = 0
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.record_usage("s1", "future", 2, date(2026, 3, 1))
    billing.record_usage("s1", "late", 3, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 300),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
    ]


def test_mid_period_changes_prorate_and_split_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="plus",
            name="Plus",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=200,
        )
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.change_plan("s1", "plus", date(2026, 1, 16))
    billing.record_usage("s1", "first", 7, date(2026, 1, 10))
    billing.record_usage("s1", "second", 12, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Plus", 3000),
        ("overage", "Overage: Basic", 200),
        ("overage", "Overage: Plus", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "plus"
    assert subscription.plan_changes == []


def test_rejected_plan_change_does_not_mutate_state(billing, store):
    store.add_plan(Plan(plan_id="plus", name="Plus", monthly_price_cents=6000))
    billing.change_plan("s1", "plus", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    previous_changes = list(subscription.plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))

    assert subscription.plan_changes == previous_changes


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 10))
