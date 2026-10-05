from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
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
    store.get_subscription("s1").plan_id = "metered"


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="x", name="X", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_completely(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_raise(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]
    assert invoice.total_cents == 3000


def test_events_are_billed_once_and_future_events_wait(billing):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 30))
    billing.record_usage("s1", "e2", 130, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[-1] == ("overage", "Overage: Metered", 100)
    assert lines(second)[-1] == ("overage", "Overage: Metered", 150)

    third = billing.generate_invoice("s1")
    assert [item.kind for item in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 110, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 50)


def test_billed_event_id_cannot_be_recorded_again(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False
    invoice = billing.generate_invoice("s1")
    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 50, date(2026, 1, 1))
    billing.record_usage("s1", "b", 10, date(2026, 1, 10))
    billing.record_usage("s1", "c", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 135),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 5235

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_multiple_changes_and_rounding(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    billing.change_plan("s1", "odd", date(2026, 1, 8))
    billing.change_plan("s1", "metered", date(2026, 1, 9))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 700),
        ("plan", "Plan: Odd", 33),
        ("plan", "Plan: Metered", 2200),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_round_half_up_on_proration(billing, store):
    store.add_plan(Plan(plan_id="half", name="Half", monthly_price_cents=45))
    store.get_subscription("s1").plan_id = "half"
    billing.change_plan("s1", "metered", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Half", 15)


def test_included_units_are_floored_per_segment(billing, store):
    store.add_plan(
        Plan(
            plan_id="tiny",
            name="Tiny",
            monthly_price_cents=0,
            included_units=10,
            overage_unit_price_cents=7,
        )
    )
    store.get_subscription("s1").plan_id = "tiny"
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.record_usage("s1", "a", 3, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Tiny", 7) in lines(invoice)


def test_late_event_attributed_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 500, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1][1] == "Overage: Metered"


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31)]
)
def test_change_outside_open_period_raises(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_raises(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_unknown_plan_raises_key_error(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_discount_credit_tax_apply_to_usage_subtotal(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
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
