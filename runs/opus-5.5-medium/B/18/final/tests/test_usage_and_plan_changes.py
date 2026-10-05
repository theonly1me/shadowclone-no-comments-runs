from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def extra_plans(store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is True
    assert billing.record_usage("s1", "e1", 50, date(2026, 1, 4)) is False


def test_duplicate_event_is_billed_once_with_original_units(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 35, date(2026, 1, 3))
    billing.record_usage("s1", "e1", 500, date(2026, 1, 4))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 50),
    ]


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 3))


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 3))


def test_usage_within_included_units_has_no_overage(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 30, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 1000)]


def test_event_at_period_end_waits_for_next_invoice(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 40, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second) == [
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 100),
    ]


def test_event_is_billed_only_once(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 1100
    assert lines(second) == [("plan", "Plan: Metered", 1000)]


def test_late_event_goes_to_first_segment_of_open_period(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 2, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_mid_cycle_change_prorates_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, store):
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 2))
    store.get_plan("basic").monthly_price_cents = 45

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Odd", 33),
        ("plan", "Plan: Basic", 44),
    ]


def test_usage_is_split_across_segments(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 20, date(2026, 1, 5))
    billing.record_usage("s1", "b", 5, date(2026, 1, 16))
    billing.record_usage("s1", "c", 60, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Metered", 50),
        ("overage", "Overage: Pro", 75),
    ]
    assert invoice.total_cents == 3625


def test_included_units_are_floored(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    billing.record_usage("s1", "a", 97, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Pro", 5) in lines(invoice)


def test_several_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_after_invoice_subscription_moves_to_final_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 3, 2)
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_not_after_previous_change_is_rejected(billing, store, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", effective_on)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_unknown_plan_is_rejected(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_change_on_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 200
    billing.record_usage("s1", "e1", 130, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("overage", 1000),
        ("discount", -200),
        ("credit", -200),
        ("tax", 160),
    ]
    assert invoice.total_cents == 1760
    assert customer.credit_balance_cents == 0
