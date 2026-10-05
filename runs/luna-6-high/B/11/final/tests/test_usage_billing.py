from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 20)) is False

    first = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]
    assert billing.record_usage("s1", "evt-1", 1, date(2026, 2, 1)) is False


def test_usage_at_period_end_waits_for_next_invoice(billing, store):
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
    billing.record_usage("s1", "evt-1", 4, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000)
    ]

    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000)
    ]


def test_late_usage_is_billed_in_current_period(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=2,
            overage_unit_price_cents=50,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "late", 5, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 150),
    ]


def test_plan_change_prorates_and_attributes_usage_by_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="old",
            name="Old",
            monthly_price_cents=3000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="new",
            name="New",
            monthly_price_cents=6000,
            included_units=200,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "old"
    billing.record_usage("s1", "before", 60, date(2026, 1, 10))
    billing.record_usage("s1", "late", 5, date(2025, 12, 20))
    billing.record_usage("s1", "after", 110, date(2026, 1, 20))
    billing.change_plan("s1", "new", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Old", 1500),
        ("plan", "Plan: New", 3000),
        ("overage", "Overage: Old", 75),
        ("overage", "Overage: New", 100),
    ]
    assert invoice.total_cents == 4675
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "new"
    assert store.get_plan_changes("s1") == []


def test_multiple_plan_changes_and_round_half_up(billing, store):
    store.add_plan(Plan("p1", "P1", 100))
    store.add_plan(Plan("p2", "P2", 100))
    store.add_plan(Plan("p3", "P3", 100))
    store.get_subscription("s1").plan_id = "p1"
    billing.change_plan("s1", "p2", date(2026, 1, 16))
    billing.change_plan("s1", "p3", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: P1", 50),
        ("Plan: P2", 17),
        ("Plan: P3", 33),
    ]


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 2))


def test_invalid_plan_change_leaves_state_unchanged(billing, store):
    store.add_plan(Plan("other", "Other", 1000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "other", date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    billing.change_plan("s1", "other", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    assert [(change.plan_id, change.effective_on) for change in store.get_plan_changes("s1")] == [
        ("other", date(2026, 1, 10))
    ]


def test_plan_change_rejects_unknown_plan(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    assert store.get_plan_changes("s1") == []
