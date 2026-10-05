from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            included_units=5,
            overage_unit_price_cents=25,
        )
    )
    assert billing.record_usage("s1", "evt-1", 8, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 100, date(2026, 1, 20)) is False
    store.get_subscription("s1").plan_id = "metered"

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("overage", 75),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 1000)
    ]


def test_late_and_future_events_are_billed_in_the_next_eligible_period(billing):
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "future", 3, date(2026, 1, 31))

    first_invoice = billing.generate_invoice("s1")
    second_invoice = billing.generate_invoice("s1")

    assert first_invoice.total_cents == 3000
    assert second_invoice.total_cents == 3000


def test_mid_period_changes_prorate_plan_and_usage_by_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6200,
            included_units=10,
            overage_unit_price_cents=30,
        )
    )
    store.add_plan(
        Plan(
            plan_id="team",
            name="Team",
            monthly_price_cents=3100,
            included_units=4,
            overage_unit_price_cents=50,
        )
    )
    billing.record_usage("s1", "old-use", 12, date(2026, 1, 5))
    billing.record_usage("s1", "new-use", 8, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2067),
        ("plan", "Plan: Team", 1033),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Pro", 150),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "team"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_change_validation_leaves_state_unchanged(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    changes = list(store.get_subscription("s1").plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    assert store.get_subscription("s1").plan_changes == changes


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 1))


def test_usage_billing_discount_credit_tax_follow_subtotal(billing, store):
    store.get_subscription("s1").plan_id = "basic"
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=0,
            overage_unit_price_cents=100,
        )
    )
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 100
    billing.record_usage("s1", "evt", 2, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -320),
        ("credit", -100),
        ("tax", 278),
    ]
