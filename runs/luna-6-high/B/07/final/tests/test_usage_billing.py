from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


def test_usage_and_plan_changes_are_prorated_and_segmented(billing, store):
    store.add_plan(
        Plan(
            plan_id="premium",
            name="Premium",
            monthly_price_cents=6000,
            included_units=60,
            overage_unit_price_cents=200,
        )
    )
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=100,
        )
    )
    billing.change_plan("s1", "premium", date(2026, 1, 16))
    billing.record_usage("s1", "first", 20, date(2026, 1, 10))
    billing.record_usage("s1", "second", 40, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Premium", 3000),
        ("overage", "Overage: Basic", 500),
        ("overage", "Overage: Premium", 2000),
    ]
    assert invoice.total_cents == 7000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []


def test_usage_events_are_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            included_units=0,
            overage_unit_price_cents=25,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    assert billing.record_usage("s1", "evt", 4, date(2026, 1, 4)) is True
    assert billing.record_usage("s1", "evt", 99, date(2030, 1, 1)) is False

    first_invoice = billing.generate_invoice("s1")
    second_invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first_invoice.line_items] == [
        ("plan", 1000),
        ("overage", 100),
    ]
    assert [(item.kind, item.amount_cents) for item in second_invoice.line_items] == [
        ("plan", 1000)
    ]
    assert billing.record_usage("s1", "evt", 1, date(2026, 2, 1)) is False


def test_late_usage_is_billed_and_period_end_usage_waits(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            overage_unit_price_cents=10,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "late", 2, date(2025, 12, 31))
    billing.record_usage("s1", "boundary", 3, date(2026, 1, 31))

    first_invoice = billing.generate_invoice("s1")
    second_invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first_invoice.line_items] == [
        ("plan", 1000),
        ("overage", 20),
    ]
    assert [(item.kind, item.amount_cents) for item in second_invoice.line_items] == [
        ("plan", 1000),
        ("overage", 30),
    ]


def test_usage_rejects_nonpositive_units_and_unknown_subscription(billing):
    with pytest.raises(ValueError):
        billing.record_usage("missing", "bad", 0, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "good", 1, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "bad", -1, date(2026, 1, 2))


def test_plan_changes_validate_without_mutating_state(billing, store):
    store.add_plan(Plan("premium", "Premium", 6000))
    store.add_plan(Plan("pro", "Pro", 9000))
    billing.change_plan("s1", "premium", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "premium", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert subscription.plan_changes == before


def test_multiple_plan_changes_apply_in_chronological_segments(billing, store):
    store.add_plan(Plan("premium", "Premium", 6000))
    store.add_plan(Plan("pro", "Pro", 9000))
    billing.change_plan("s1", "premium", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Basic", 1000),
        ("Plan: Premium", 2000),
        ("Plan: Pro", 3000),
    ]
    assert store.get_subscription("s1").plan_id == "pro"


def test_discount_credit_and_tax_apply_after_usage_subtotal(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            overage_unit_price_cents=100,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    store.get_customer("c1").tax_rate_percent = Decimal("10")
    store.get_customer("c1").credit_balance_cents = 200
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    billing.record_usage("s1", "usage", 5, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("overage", 500),
        ("discount", -150),
        ("credit", -200),
        ("tax", 115),
    ]
    assert invoice.total_cents == 1265
    assert store.get_customer("c1").credit_balance_cents == 0
