from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


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
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="team", name="Team", monthly_price_cents=9000))
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_for_new_event(billing, plans):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True


def test_record_usage_is_idempotent_per_event_id(billing, plans):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_same_event_id_on_other_subscription_is_separate(billing, plans):
    from ledger.models import Subscription

    plans.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, plans, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage(billing, plans):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_on_period_end_is_billed_next_period(billing, plans):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert second.period_start == date(2026, 1, 31)
    assert lines(second)[-1] == ("overage", "Overage: Metered", 100)


def test_usage_is_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[-1] == ("overage", "Overage: Metered", 100)
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, plans):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 130, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 150)


def test_plan_change_prorates_and_splits_usage(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 30, date(2026, 1, 10))
    billing.record_usage("s1", "b", 300, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Pro", 200),
    ]
    assert invoice.total_cents == 5200


def test_included_units_are_floored_per_segment(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.record_usage("s1", "a", 24, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 700),
        ("plan", "Plan: Pro", 4600),
        ("overage", "Overage: Metered", 5),
    ]


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    billing.change_plan("s1", "odd", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 100),
        ("plan", "Plan: Odd", 44),
    ]


def test_multiple_changes_and_late_event_in_first_segment(billing, plans):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.change_plan("s1", "team", date(2026, 2, 20))
    billing.change_plan("s1", "metered", date(2026, 2, 25))
    billing.record_usage("s1", "late", 50, date(2026, 1, 20))
    billing.record_usage("s1", "early", 50, date(2026, 2, 1))
    billing.record_usage("s1", "team", 7, date(2026, 2, 22))
    billing.record_usage("s1", "end", 30, date(2026, 3, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 1500),
        ("plan", "Plan: Metered", 500),
        ("overage", "Overage: Metered", 335),
        ("overage", "Overage: Team", 0),
        ("overage", "Overage: Metered", 70),
    ]
    subscription = plans.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.period_start == date(2026, 3, 2)


def test_plan_after_invoice_is_last_plan_and_changes_are_cleared(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")

    assert plans.get_subscription("s1").plan_id == "pro"
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]
    billing.change_plan("s1", "team", date(2026, 3, 10))


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, plans, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_must_be_after_previous_change(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")
    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
    ]


def test_change_to_current_plan_is_rejected(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    billing.change_plan("s1", "metered", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, plans):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


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
