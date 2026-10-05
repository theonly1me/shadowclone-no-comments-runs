from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def metered(store):
    basic = store.get_plan("basic")
    basic.included_units = 100
    basic.overage_unit_price_cents = 5
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="mini", name="Mini", monthly_price_cents=900))
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent_by_event_id(billing, metered):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    metered.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", -3, date(2026, 1, 5))

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage(billing, metered):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_usage_waits_and_is_billed_once(billing, metered):
    billing.record_usage("s1", "now", 101, date(2026, 1, 30))
    billing.record_usage("s1", "later", 110, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1:] == [("overage", "Overage: Basic", 5)]
    assert lines(second)[1:] == [("overage", "Overage: Basic", 50)]
    assert lines(third) == [("plan", "Plan: Basic", 3000)]


def test_late_usage_is_billed_in_open_period(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 120, date(2026, 1, 10))
    billing.record_usage("s1", "on-time", 10, date(2026, 2, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1:] == [("overage", "Overage: Basic", 150)]


def test_plan_change_prorates_and_splits_usage(billing, metered):
    billing.record_usage("s1", "a", 50, date(2026, 1, 5))
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 85),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 5185


def test_late_usage_goes_to_first_segment(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 2, 5))

    invoice = billing.generate_invoice("s1")

    basic_included = 100 * 5 // 30
    assert ("overage", "Overage: Basic", (40 - basic_included) * 5) in lines(invoice)
    assert [item.kind for item in invoice.line_items].count("overage") == 1


def test_multiple_changes_in_one_period(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "mini", date(2026, 1, 21))
    billing.change_plan("s1", "basic", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Mini", 150),
        ("plan", "Plan: Basic", 500),
    ]


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="cheap", name="Cheap", monthly_price_cents=15))
    store.get_plan("basic").monthly_price_cents = 15
    billing.change_plan("s1", "cheap", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1, 15]


def test_plan_carries_over_and_changes_are_cleared(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 1)],
)
def test_change_plan_rejects_dates_outside_period(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)

    assert metered.get_subscription("s1").plan_changes == []


def test_change_plan_must_be_after_previous_change(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "mini", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "mini", date(2026, 1, 5))

    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_change_plan_to_current_plan_is_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    billing.change_plan("s1", "basic", date(2026, 1, 20))


def test_change_plan_unknown_plan_or_subscription(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))

    assert metered.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_usage(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
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
