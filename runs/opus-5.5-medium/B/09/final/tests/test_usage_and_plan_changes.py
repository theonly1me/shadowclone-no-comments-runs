from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def plans(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent(billing, plans):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, plans):
    plans.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_event_waits_for_its_period(billing, plans):
    billing.record_usage("s1", "e1", 110, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Metered", 50)


def test_late_event_is_billed_on_open_period_once(billing, plans):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 120, date(2026, 1, 10))

    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(second)[1] == ("overage", "Overage: Metered", 100)
    assert [item.kind for item in third.line_items] == ["plan"]


def test_change_plan_prorates_and_splits_usage(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 50, date(2026, 1, 10))
    billing.record_usage("s1", "e2", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 333),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 85),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 4518


def test_multiple_changes_and_late_event_goes_to_first_segment(billing, plans):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.change_plan("s1", "basic", date(2026, 2, 20))
    billing.record_usage("s1", "late", 40, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 333),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Metered", 35),
    ]


def test_after_invoice_plan_is_final_and_changes_cleared(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    billing.generate_invoice("s1")
    subscription = store.get_subscription("s1")
    nxt = billing.generate_invoice("s1")

    assert subscription.plan_id == "pro"
    assert lines(nxt) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2026, 1, 31), date(2025, 12, 1)]
)
def test_change_plan_outside_period_rejected(billing, plans, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_plan_must_follow_previous_change(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 15))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_rejected(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    billing.change_plan("s1", "metered", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, plans, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_discount_credit_tax_apply_to_usage_subtotal(billing, plans, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 200
    billing.record_usage("s1", "e1", 300, date(2026, 1, 2))

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
