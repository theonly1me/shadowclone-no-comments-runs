from datetime import date

import pytest

from ledger.models import Plan


def test_usage_idempotency_validation_and_unknown_subscription(billing):
    assert billing.record_usage("s1", "evt-1", 3, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2030, 1, 1)) is False

    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt-2", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt-3", 1, date(2026, 1, 5))


def test_usage_event_boundaries_and_late_usage(billing, store):
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
    billing.record_usage("s1", "late", 1, date(2025, 12, 20))
    billing.record_usage("s1", "inside", 4, date(2026, 1, 30))
    billing.record_usage("s1", "boundary", 20, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 300),
    ]

    second = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 1800),
    ]


def test_plan_changes_split_price_allowance_and_usage(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic-metered",
            name="Basic",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=100,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=20,
            overage_unit_price_cents=200,
        )
    )
    store.get_subscription("s1").plan_id = "basic-metered"

    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "basic-use", 6, date(2026, 1, 15))
    billing.record_usage("s1", "pro-use", 12, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 400),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert store.get_plan_changes("s1") == []


def test_plan_change_validation_leaves_changes_untouched(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    for plan_id, effective_on in [
        ("basic", date(2026, 1, 10)),  # not later than the previous change
        ("pro", date(2026, 1, 20)),  # already current
        ("basic", date(2026, 1, 31)),  # period end is exclusive
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)

    assert [(change.plan_id, change.effective_on) for change in store.get_plan_changes("s1")] == [
        ("pro", date(2026, 1, 10))
    ]

    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    assert len(store.get_plan_changes("s1")) == 1
