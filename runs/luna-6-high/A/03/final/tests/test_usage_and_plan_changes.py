from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


def test_usage_is_deduplicated_and_billed_as_overage(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=10, overage_unit_price_cents=25))
    assert billing.record_usage("s1", "evt-1", 12, date(2026, 1, 10))
    assert not billing.record_usage("s1", "evt-1", 900, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 50),
    ]
    assert not any(item.kind == "overage" for item in billing.generate_invoice("s1").line_items)


def test_usage_validation_and_duplicate_payload_precedence(billing):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "evt", 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "evt", 0, date(2026, 1, 1))

    assert billing.record_usage("s1", "evt", 1, date(2026, 1, 1))
    assert not billing.record_usage("s1", "evt", 0, date(2026, 2, 1))


def test_usage_beyond_period_waits_and_late_usage_bills_next_invoice(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=0, overage_unit_price_cents=10))
    assert billing.record_usage("s1", "future", 2, date(2026, 2, 1))
    assert billing.record_usage("s1", "late", 3, date(2025, 12, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in first.line_items] == [
        ("plan", 3000),
        ("overage", 30),
    ]
    assert [(item.kind, item.amount_cents) for item in second.line_items] == [
        ("plan", 3000),
        ("overage", 20),
    ]


def test_midperiod_changes_prorate_and_allocate_usage_per_segment(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000, included_units=10, overage_unit_price_cents=20))
    billing.record_usage("s1", "basic-usage", 8, date(2026, 1, 5))
    billing.record_usage("s1", "pro-usage", 8, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description, item.amount_cents) for item in invoice.line_items] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Pro", 60),
    ]
    assert store.get_subscription("s1").plan_id == "pro"


def test_multiple_plan_changes_validate_without_mutating_on_rejection(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    store.add_plan(Plan("team", "Team", 9000))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    billing.change_plan("s1", "team", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [(item.description, item.amount_cents) for item in invoice.line_items] == [
        ("Plan: Basic", 900),
        ("Plan: Pro", 2000),
        ("Plan: Team", 3300),
    ]
    assert store.get_subscription("s1").plan_id == "team"


def test_discount_credit_and_tax_apply_to_plan_and_usage_subtotal(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=0, overage_unit_price_cents=100))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "usage", 2, date(2026, 1, 4))

    invoice = billing.generate_invoice("s1", "TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -320),
        ("credit", -500),
        ("tax", 238),
    ]
