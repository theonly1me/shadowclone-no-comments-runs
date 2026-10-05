from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription

# The fixture subscription "s1" bills [2026-01-01, 2026-01-31): 30 days.


@pytest.fixture(autouse=True)
def usage_plans(store):
    store.add_plan(
        Plan(
            plan_id="small",
            name="Small",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="big",
            name="Big",
            monthly_price_cents=9000,
            included_units=300,
            overage_unit_price_cents=5,
        )
    )
    store.get_subscription("s1").plan_id = "small"


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Small", 3000),
        ("overage", "Overage: Small", 100),
    ]


def test_same_event_id_on_another_subscription_is_separate(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "small", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))

    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Small", 3000)]


def test_event_on_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 35, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Small", 50) in lines(second)


def test_events_are_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert ("overage", "Overage: Small", 100) in lines(first)
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")  # now open: [2026-01-31, 2026-03-02)
    billing.record_usage("s1", "late", 45, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Small", 150) in lines(invoice)


def test_plan_change_prorates_and_splits_usage(billing, store):
    billing.record_usage("s1", "late", 5, date(2025, 12, 20))
    billing.record_usage("s1", "e1", 15, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 250, date(2026, 1, 11))
    billing.change_plan("s1", "big", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    # Small: 10 days, includes 10 units, 20 used. Big: 20 days, includes 200.
    assert lines(invoice) == [
        ("plan", "Plan: Small", 1000),
        ("plan", "Plan: Big", 6000),
        ("overage", "Overage: Small", 100),
        ("overage", "Overage: Big", 250),
    ]
    assert invoice.total_cents == 7350

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "big"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_next_period_bills_the_new_plan_in_full(billing):
    billing.change_plan("s1", "big", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Big", 9000)]


def test_several_changes_in_one_period(billing):
    billing.change_plan("s1", "big", date(2026, 1, 2))
    billing.change_plan("s1", "small", date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    # 1 day of Small (100), 1 day of Big (300), 28 days of Small (2800).
    assert lines(invoice) == [
        ("plan", "Plan: Small", 100),
        ("plan", "Plan: Big", 300),
        ("plan", "Plan: Small", 2800),
    ]


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="one", name="One", monthly_price_cents=1))
    store.get_subscription("s1").plan_id = "one"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # 1 cent * 15 / 30 = 0.5 rounds up to 1; 3000 * 15 / 30 = 1500.
    assert lines(invoice) == [
        ("plan", "Plan: One", 1),
        ("plan", "Plan: Basic", 1500),
    ]


def test_included_units_are_floored_per_segment(billing):
    # Small includes 30 * 7 // 30 = 7 units over 7 days.
    billing.change_plan("s1", "big", date(2026, 1, 8))
    billing.record_usage("s1", "e1", 8, date(2026, 1, 7))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Small", 10) in lines(invoice)


@pytest.mark.parametrize(
    "plan_id, effective_on, error",
    [
        ("big", date(2026, 1, 1), ValueError),
        ("big", date(2025, 12, 31), ValueError),
        ("big", date(2026, 1, 31), ValueError),
        ("small", date(2026, 1, 10), ValueError),
        ("missing", date(2026, 1, 10), KeyError),
    ],
)
def test_invalid_plan_change_is_rejected(billing, store, plan_id, effective_on, error):
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, effective_on)

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "small"
    assert subscription.plan_changes == []


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_must_be_after_previous_change(billing, store, effective_on):
    billing.change_plan("s1", "big", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "small", effective_on)

    assert store.get_subscription("s1").plan_changes == [(date(2026, 1, 10), "big")]


def test_change_to_plan_current_after_previous_change_is_rejected(billing):
    billing.change_plan("s1", "big", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "big", date(2026, 1, 20))


def test_change_plan_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "big", date(2026, 1, 10))


def test_discount_credit_and_tax_apply_to_usage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 130, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # Subtotal 3000 + 1000 = 4000; discount 400; credit 500; tax on 3100.
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
