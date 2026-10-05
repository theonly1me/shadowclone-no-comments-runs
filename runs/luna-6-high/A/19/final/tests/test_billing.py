from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


def test_flat_invoice_has_one_plan_line(billing):
    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]
    assert invoice.total_cents == 3000


def test_invoice_is_saved(billing, store):
    invoice = billing.generate_invoice("s1")

    assert store.get_invoice(invoice.invoice_id) == invoice


def test_period_advances_by_the_same_length(billing, store):
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_discount_credit_and_tax_order(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("discount", -300),
        ("credit", -500),
        ("tax", 220),
    ]
    assert invoice.total_cents == 2420
    assert customer.credit_balance_cents == 0


def test_credit_larger_than_invoice_leaves_a_balance(billing, store):
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000

    invoice = billing.generate_invoice("s1")

    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000


def test_usage_is_idempotent_and_bills_overage(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=25,
        )
    )
    assert billing.record_usage("s1", "evt-1", 13, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 20)) is False

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 75),
    ]
    assert invoice.total_cents == 3075


def test_usage_at_period_end_waits_until_a_later_invoice(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            overage_unit_price_cents=25,
        )
    )
    billing.record_usage("s1", "evt-late", 3, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 75),
    ]


def test_plan_changes_prorate_plans_and_attribute_usage_to_segments(billing, store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=15,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=30,
            overage_unit_price_cents=20,
        )
    )
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "evt-basic", 20, date(2026, 1, 10))
    billing.record_usage("s1", "evt-pro", 40, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 130),
        ("overage", "Overage: Pro", 500),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert store.get_plan_changes("s1") == []


def test_rejected_plan_change_leaves_changes_unchanged(billing, store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=5000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    before = store.get_plan_changes("s1")

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 9))

    assert store.get_plan_changes("s1") == before
