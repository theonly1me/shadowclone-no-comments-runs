from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=2,
            overage_unit_price_cents=75,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    assert billing.record_usage("s1", "e1", 4, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "e1", 100, date(2026, 1, 31)) is False

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 150),
    ]
    assert billing.generate_invoice("s1").line_items == [
        type(invoice.line_items[0])("plan", "Plan: Metered", 3000)
    ]


def test_plan_changes_prorate_prices_and_split_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="premium",
            name="Premium",
            monthly_price_cents=6000,
            included_units=10,
            overage_unit_price_cents=200,
        )
    )
    store.get_plan("basic").included_units = 5
    store.get_plan("basic").overage_unit_price_cents = 100
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "old", 3, date(2026, 1, 15))
    billing.record_usage("s1", "new", 8, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Premium", 3000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Premium", 600),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.period_start == date(2026, 1, 31)
    assert billing.generate_invoice("s1").line_items[0].description == "Plan: Premium"


def test_late_usage_uses_first_segment_and_future_period_usage_waits(billing, store):
    store.add_plan(Plan("premium", "Premium", 6000))
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "late", 2, date(2025, 12, 20))
    billing.record_usage("s1", "future", 2, date(2026, 1, 31))
    store.get_plan("basic").overage_unit_price_cents = 10
    store.get_plan("premium").overage_unit_price_cents = 20

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 3000),
        ("overage", 20),
    ]
    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 6000),
        ("overage", 40),
    ]


def test_invalid_usage_and_plan_changes_do_not_mutate_state(billing, store):
    store.add_plan(Plan("premium", "Premium", 6000))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", date(2026, 1, 1))
    billing.change_plan("s1", "premium", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")
    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Premium",
    ]
