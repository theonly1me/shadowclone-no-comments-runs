from datetime import date

import pytest

from ledger.models import Plan


def test_usage_and_multiple_plan_changes_are_billed_by_segment(billing, store):
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=30, overage_unit_price_cents=50)
    )
    store.add_plan(
        Plan("team", "Team", 9000, included_units=0, overage_unit_price_cents=25)
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 100

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.record_usage("s1", "late", 5, date(2025, 12, 31))
    billing.record_usage("s1", "pro-use", 12, date(2026, 1, 11))
    billing.record_usage("s1", "team-use", 2, date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 3000),
        ("overage", "Overage: Basic", 200),
        ("overage", "Overage: Pro", 100),
        ("overage", "Overage: Team", 50),
    ]
    assert invoice.total_cents == 6350
    assert store.get_subscription("s1").plan_id == "team"
    assert store.get_subscription("s1").period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_usage_is_idempotent_and_future_period_events_remain_pending(billing, store):
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 31)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 1, 1)) is False
    invoice = billing.generate_invoice("s1")
    assert invoice.total_cents == 3000

    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000),
        ("overage", 0),
    ]
    assert billing.record_usage("s1", "event", 1, date(2026, 2, 1)) is False


def test_usage_validation_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", 0, date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 1))


def test_invalid_plan_changes_do_not_mutate_state(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    assert store.get_plan_changes("s1") == []


def test_change_date_order_and_duplicate_current_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 2, 1))
    billing.change_plan("s1", "basic", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 25))
    assert len(store.get_plan_changes("s1")) == 2
