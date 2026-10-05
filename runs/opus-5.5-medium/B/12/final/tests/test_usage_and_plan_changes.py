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
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_fields_default_to_zero():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent_per_subscription(billing, plans):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_same_event_id_on_other_subscription_is_recorded(billing, plans):
    plans.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_rejects_non_positive_units(billing, plans):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", -3, date(2026, 1, 5))

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_has_no_overage(billing, plans):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_on_period_end_waits_for_next_invoice(billing, plans):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Metered", 5)


def test_event_is_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 110, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 3050
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_billed_on_open_period_in_first_segment(billing, plans):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2026, 1, 15))
    billing.record_usage("s1", "e2", 40, date(2026, 2, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 285),
    ]


def test_plan_change_prorates_by_segment(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, plans):
    billing.change_plan("s1", "odd", date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 2900),
        ("plan", "Plan: Odd", 2),
    ]


def test_usage_attributed_to_segments_with_prorated_included_units(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))
    billing.record_usage("s1", "e2", 210, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Pro", 20),
    ]


def test_multiple_changes_in_one_period(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))
    billing.record_usage("s1", "e1", 34, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 5),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_subscription_moves_to_final_plan_and_changes_clear(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_rejected(billing, plans, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)

    assert store.get_subscription("s1").plan_changes == []


def test_change_must_be_after_previous_change(billing, plans, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))

    subscription = store.get_subscription("s1")
    assert len(subscription.plan_changes) == 1
    assert subscription.plan_id == "metered"


def test_change_to_current_plan_rejected(billing, plans, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_unknown_plan_or_subscription(billing, plans, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))

    assert store.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_subtotal_with_overage(billing, plans, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 15))

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
