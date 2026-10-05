from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


def test_usage_is_idempotent_and_billed_once(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, 0, 100))
    assert billing.record_usage("s1", "evt-1", 5, date(2026, 1, 10)) is True
    assert billing.record_usage("s1", "evt-1", 99, date(2026, 1, 30)) is False

    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
    ]
    next_invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in next_invoice.line_items] == [
        ("plan", 3000)
    ]


def test_usage_timing_and_late_events(billing, store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=10, overage_unit_price_cents=100)
    )
    assert billing.record_usage("s1", "future", 3, date(2026, 1, 31))
    assert billing.record_usage("s1", "late", 12, date(2025, 12, 1))
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
    ]
    following = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in following.line_items] == [
        ("plan", 3000),
    ]


def test_plan_changes_prorate_and_attribute_usage_by_segment(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, 10, 100))
    store.add_plan(Plan("pro", "Pro", 6200, 5, 200))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 4, date(2026, 1, 10))
    billing.record_usage("s1", "b", 8, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3100),
        ("overage", "Overage: Pro", 1200),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)


def test_multiple_plan_changes_and_invoice_adjustment_order(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, 0, 100))
    store.add_plan(Plan("team", "Team", 9000, 0, 100))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.record_usage("s1", "u1", 1, date(2026, 1, 1))
    billing.record_usage("s1", "u2", 2, date(2026, 1, 12))
    billing.record_usage("s1", "u3", 3, date(2026, 1, 25))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("plan", 2000),
        ("plan", 3000),
        ("overage", 0),
        ("overage", 200),
        ("overage", 300),
        ("discount", -650),
        ("credit", -500),
        ("tax", 535),
    ]


@pytest.mark.parametrize(
    "effective_on,new_plan_id",
    [(date(2026, 1, 1), "pro"), (date(2026, 1, 31), "pro")],
)
def test_rejected_plan_change_does_not_change_billing(billing, store, effective_on, new_plan_id):
    store.add_plan(Plan("pro", "Pro", 6000))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    invoice = billing.generate_invoice("s1")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [("plan", 3000)]


def test_invalid_usage_and_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 1))


def test_duplicate_usage_ignores_invalid_replacement(billing):
    assert billing.record_usage("s1", "evt", 2, date(2026, 1, 1))
    assert not billing.record_usage("s1", "evt", 0, date(2026, 1, 2))
