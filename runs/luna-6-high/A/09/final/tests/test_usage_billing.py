from datetime import date

import pytest

from ledger.models import Plan


def test_usage_events_are_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=2,
            overage_unit_price_cents=100,
        )
    )
    subscription = store.get_subscription("s1")
    subscription.plan_id = "metered"

    assert billing.record_usage("s1", "evt-1", 5, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 300),
    ]
    assert billing.generate_invoice("s1").line_items == [
        invoice.line_items[0]
    ]


def test_usage_at_period_end_is_deferred_and_late_usage_is_invoiced(billing, store):
    store.add_plan(
        Plan("metered", "Metered", 3000, included_units=10, overage_unit_price_cents=50)
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "boundary", 4, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [item.kind for item in first.line_items] == ["plan"]

    billing.record_usage("s1", "late", 13, date(2025, 12, 1))
    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 350),
    ]


def test_plan_changes_prorate_and_usage_is_segmented(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=10, overage_unit_price_cents=100)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=200)
    )
    store.add_plan(
        Plan("team", "Team", 9000, included_units=30, overage_unit_price_cents=300)
    )
    billing.record_usage("s1", "basic-usage", 8, date(2026, 1, 10))
    billing.record_usage("s1", "pro-usage", 15, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.change_plan("s1", "team", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 1500),
        ("overage", "Overage: Basic", 300),
        ("overage", "Overage: Pro", 1800),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "team"
    assert subscription.period_start == date(2026, 1, 31)


def test_rejected_plan_change_does_not_mutate_state(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    assert len(store.get_plan_changes("s1")) == 1


def test_usage_requires_positive_units_and_known_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))


def test_plan_change_unknown_plan_raises_key_error_without_mutation(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    assert store.get_plan_changes("s1") == []
