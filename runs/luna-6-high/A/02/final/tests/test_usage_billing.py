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
            overage_unit_price_cents=100,
        )
    )
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 5, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 11)) is False
    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 300),
    ]
    assert billing.record_usage("s1", "evt-1", 8, date(2026, 2, 1)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


def test_usage_validation_and_future_event_waits_for_next_period(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 5))

    assert billing.record_usage("s1", "future", 1, date(2026, 1, 31))
    assert billing.generate_invoice("s1").total_cents == 3000
    assert [
        (item.kind, item.amount_cents)
        for item in billing.generate_invoice("s1").line_items
    ] == [("plan", 3000), ("overage", 0)]


def test_change_plan_prorates_and_applies_usage_to_segments(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic-metered",
            name="Basic",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "basic-metered"
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "late", 1, date(2025, 12, 20))
    billing.record_usage("s1", "before", 8, date(2026, 1, 5))
    billing.record_usage("s1", "at-change", 25, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 150),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_subscription("s1").period_start == date(2026, 1, 31)


def test_plan_changes_are_validated_and_multiple_changes_are_ordered(billing, store):
    store.add_plan(Plan("silver", "Silver", 4000))
    store.add_plan(Plan("gold", "Gold", 5000))
    billing.change_plan("s1", "silver", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "silver", date(2026, 1, 12))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "gold", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "gold", date(2026, 1, 31))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "absent", date(2026, 1, 15))

    billing.change_plan("s1", "gold", date(2026, 1, 20))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 900),
        ("plan", 1333),
        ("plan", 1833),
    ]
    assert store.get_subscription("s1").plan_id == "gold"
