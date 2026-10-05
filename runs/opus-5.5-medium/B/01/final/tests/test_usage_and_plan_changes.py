from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def usage_plans(store):
    basic = store.get_plan("basic")
    basic.included_units = 30
    basic.overage_unit_price_cents = 5
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=100,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="lite", name="Lite", monthly_price_cents=1000))


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
    ]


def test_same_event_id_on_another_subscription_is_separate(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -3])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_usage_waits_for_its_period_and_is_billed_once(billing):
    billing.record_usage("s1", "now", 31, date(2026, 1, 30))
    billing.record_usage("s1", "later", 35, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1:] == [("overage", "Overage: Basic", 5)]
    assert lines(second)[1:] == [("overage", "Overage: Basic", 25)]
    assert lines(third) == [("plan", "Plan: Basic", 3000)]


def test_late_usage_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1:] == [("overage", "Overage: Basic", 50)]


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 15, date(2026, 1, 10))
    billing.record_usage("s1", "b", 70, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 25),
        ("overage", "Overage: Pro", 8),
    ]
    assert invoice.total_cents == 5033
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_multiple_changes_and_late_event_in_first_segment(billing, store):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 20, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 2, 3))
    billing.change_plan("s1", "lite", date(2026, 2, 13))
    billing.record_usage("s1", "x", 1, date(2026, 2, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 300),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Lite", 567),
        ("overage", "Overage: Basic", 90),
    ]
    assert store.get_subscription("s1").plan_id == "lite"


def test_rounding_and_floor_of_included_units(billing, store):
    store.add_plan(
        Plan(
            plan_id="odd",
            name="Odd",
            monthly_price_cents=1001,
            included_units=10,
            overage_unit_price_cents=1,
        )
    )
    billing.change_plan("s1", "odd", date(2026, 1, 16))
    billing.record_usage("s1", "a", 5, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Odd", 501),
    ]


def test_discount_credit_tax_apply_to_usage_subtotal(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e", 230, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_must_be_strictly_inside_period(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "lite", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "lite", date(2026, 1, 5))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    billing.change_plan("s1", "basic", date(2026, 1, 20))

    assert store.get_subscription("s1").plan_id == "basic"


def test_change_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []
