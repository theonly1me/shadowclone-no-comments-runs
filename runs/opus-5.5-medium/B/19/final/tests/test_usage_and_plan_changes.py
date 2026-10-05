from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def usage_plans(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=3001))
    store.get_subscription("s1").plan_id = "metered"


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is True
    assert billing.record_usage("s1", "e1", 500, date(2026, 1, 4)) is False


def test_duplicate_event_is_ignored_when_billing(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 3))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 3)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 3))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 3))


def test_usage_within_allowance_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_event_on_or_after_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 35, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 50),
    ]


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 35, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan", "overage"]
    assert [item.kind for item in second.line_items] == ["plan"]
    assert billing.record_usage("s1", "e1", 35, date(2026, 2, 10)) is False


def test_late_event_is_billed_on_open_period_in_first_segment(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 25, date(2026, 1, 15))
    billing.record_usage("s1", "now", 20, date(2026, 2, 5))
    billing.change_plan("s1", "pro", date(2026, 2, 15))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Metered", 300),
    ]


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, store):
    billing.record_usage("s1", "e1", 15, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 70, date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 50),
        ("overage", "Overage: Pro", 20),
    ]
    assert invoice.total_cents == 5070
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_plan_changes_are_cleared_after_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_several_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_proration_rounds_half_up(billing):
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1500),
        ("plan", "Plan: Odd", 1501),
    ]


def test_included_units_are_floored_per_segment(billing):
    billing.record_usage("s1", "e1", 10, date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 10) in lines(invoice)


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")
    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
    ]


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_discount_credit_and_tax_apply_to_usage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 80, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice
