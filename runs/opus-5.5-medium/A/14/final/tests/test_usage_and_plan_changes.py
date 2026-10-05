from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription

# The fixture period is [2026-01-01, 2026-01-31): 30 days.


@pytest.fixture
def plans(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=1000,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1000))
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent(billing, plans):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, plans):
    plans.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_rejects_bad_input(billing, plans):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("missing", "e1", 1, date(2026, 1, 5))
    # A rejected call does not reserve the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_future_usage_waits_for_its_period_and_bills_once(billing, plans):
    billing.record_usage("s1", "now", 101, date(2026, 1, 30))
    billing.record_usage("s1", "later", 110, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1:] == [("overage", "Overage: Metered", 5)]
    assert lines(second)[1:] == [("overage", "Overage: Metered", 50)]
    assert lines(third) == [("plan", "Plan: Metered", 3000)]


def test_late_event_is_billed_on_the_open_period(billing, plans):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 120, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[1:] == [("overage", "Overage: Metered", 100)]


def test_plan_change_prorates_and_splits_usage(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered segment: 10 days, includes 100*10//30 = 33 units.
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    # Pro segment: 20 days, includes 1000*20//30 = 666 units.
    billing.record_usage("s1", "b", 700, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Pro", 68),
    ]
    assert invoice.total_cents == 5103


def test_proration_rounds_half_up_per_segment(billing, plans):
    billing.change_plan("s1", "odd", date(2026, 1, 8))

    invoice = billing.generate_invoice("s1")

    # 3000*7/30 = 700, 1000*23/30 = 766.67
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 700),
        ("plan", "Plan: Odd", 767),
    ]


def test_late_event_goes_to_first_segment(billing, plans):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    # 10-day metered segment includes 33 units.
    assert ("overage", "Overage: Metered", 85) in lines(invoice)


def test_multiple_changes_and_state_after_invoice(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
    ]
    subscription = plans.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []

    billing.change_plan("s1", "pro", date(2026, 2, 5))
    billing.generate_invoice("s1")
    assert subscription.plan_id == "pro"


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),
        ("pro", date(2026, 1, 31)),
        ("pro", date(2025, 12, 31)),
        ("pro", date(2026, 1, 11)),
        ("pro", date(2026, 1, 5)),
        ("odd", date(2026, 1, 20)),
    ],
)
def test_invalid_plan_change_is_rejected_without_side_effects(
    billing, plans, plan_id, effective_on
):
    billing.change_plan("s1", "odd", date(2026, 1, 11))
    before = list(plans.get_subscription("s1").plan_changes)

    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert plans.get_subscription("s1").plan_changes == before


def test_change_to_current_plan_or_unknown_plan_is_rejected(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "pro", date(2026, 1, 10))
    assert plans.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, plans):
    plans.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = plans.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
