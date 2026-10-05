from datetime import date

import pytest

from ledger.models import Plan


def test_usage_events_are_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=4,
            overage_unit_price_cents=25,
        )
    )
    assert billing.record_usage("s1", "evt-1", 6, date(2026, 1, 12)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False
    assert billing.record_usage("s1", "evt-1", 0, date(2026, 1, 20)) is False

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]

    # A late event is charged on the current period, while an event at the
    # exclusive period end remains pending for the following invoice.
    billing.record_usage("s1", "late", 2, date(2026, 1, 30))
    billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 25),
    ]


def test_plan_changes_prorate_and_attribute_usage_to_segments(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            overage_unit_price_cents=25,
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
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "old-plan-use", 8, date(2026, 1, 10))
    billing.record_usage("s1", "new-plan-use", 25, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 200),
        ("overage", "Overage: Pro", 150),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_rejected_changes_and_usage_leave_state_unchanged(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 5))
    assert store.get_subscription("s1").plan_changes == []


def test_multiple_plan_changes_must_be_strictly_chronological(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    store.add_plan(Plan("team", "Team", 9000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 10))
    billing.change_plan("s1", "team", date(2026, 1, 20))
    assert [change.plan_id for change in store.get_subscription("s1").plan_changes] == [
        "pro",
        "team",
    ]
