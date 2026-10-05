from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def metered(store):
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


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_returns_true_then_false_for_duplicates(billing, metered):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_when_billing(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_are_rejected(billing, metered, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_has_no_overage_line(billing, metered):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]
    assert invoice.total_cents == 3000


def test_future_event_waits_for_its_period_and_is_billed_once(billing, metered):
    billing.record_usage("s1", "now", 110, date(2026, 1, 30))
    billing.record_usage("s1", "later", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert first.total_cents == 3000 + 10 * 5
    assert second.total_cents == 3000 + 20 * 5
    assert third.total_cents == 3000


def test_late_event_is_billed_on_open_period(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 101, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 5)


def test_single_plan_change_prorates_and_splits_allowance(billing, metered):
    billing.record_usage("s1", "a", 60, date(2026, 1, 5))
    billing.record_usage("s1", "b", 400, date(2026, 1, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 5 * (60 - 33)),
        ("overage", "Overage: Pro", 2 * (400 - 200)),
    ]
    assert invoice.total_cents == 1000 + 4000 + 135 + 400


def test_late_event_goes_to_first_segment(billing, metered):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 60, date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 2, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 5 * (60 - 33)) in lines(invoice)


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    store.add_plan(Plan(plan_id="free", name="Free", monthly_price_cents=0))
    billing.change_plan("s1", "free", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Odd", 501), ("plan", "Plan: Free", 0)]


def test_multiple_changes_and_plan_after_invoice(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.change_plan("s1", "metered", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1000, 2000, 1500, 500]
    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_after_invoice_is_last_change(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    assert metered.get_subscription("s1").plan_id == "pro"
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)]
)
def test_change_outside_open_period_is_rejected(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert metered.get_subscription("s1").plan_changes == []


@pytest.mark.parametrize("effective_on", [date(2026, 1, 5), date(2026, 1, 10)])
def test_change_not_after_previous_is_rejected(billing, metered, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", effective_on)
    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 15))
    billing.change_plan("s1", "metered", date(2026, 1, 15))


def test_change_to_unknown_plan_is_rejected(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    assert metered.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_usage(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
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
