from datetime import date

import pytest

from ledger.models import Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    assert billing.record_usage("s1", "evt-1", 4, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 20)) is False
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=2, overage_unit_price_cents=50)
    )

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 100),
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000)
    ]
    assert billing.record_usage("s1", "evt-1", 1, date(2026, 3, 1)) is False


def test_usage_outside_open_period_waits_and_late_usage_is_charged(billing, store):
    billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=1, overage_unit_price_cents=25)
    )

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 25),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]


def test_mid_period_change_prorates_and_splits_usage(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, 6, 100))
    store.add_plan(Plan("pro", "Pro", 6000, 10, 100))
    store.add_plan(Plan("team", "Team", 9000, 20, 200))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.record_usage("s1", "a", 4, date(2026, 1, 5))
    billing.record_usage("s1", "b", 7, date(2026, 1, 15))
    billing.record_usage("s1", "c", 25, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 3000),
        ("overage", "Overage: Basic", 200),
        ("overage", "Overage: Pro", 400),
        ("overage", "Overage: Team", 3800),
    ]
    assert store.get_subscription("s1").plan_id == "team"
    assert store.get_plan_changes("s1") == []


def test_rejected_plan_changes_do_not_mutate_state(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = list(store.get_plan_changes("s1"))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert store.get_plan_changes("s1") == before


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "x", 1, date(2026, 1, 1))
