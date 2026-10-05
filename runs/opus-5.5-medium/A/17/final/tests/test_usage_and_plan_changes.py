from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# The fixture subscription s1 is on "basic" for [2026-01-01, 2026-01-31): 30 days.


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
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# Usage events


def test_record_usage_returns_true_then_false_for_duplicate(billing, metered):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_on_invoice(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 500, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    from ledger.models import Subscription

    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_raise(billing, metered, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # Nothing was recorded, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_adds_no_overage_line(billing, metered):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_on_period_end_waits_for_next_invoice(billing, metered):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 31))
    billing.record_usage("s1", "e2", 110, date(2026, 1, 30))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[1:] == [("overage", "Overage: Metered", 50)]
    assert lines(second)[1:] == [("overage", "Overage: Metered", 100)]


def test_event_is_billed_only_once(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, metered):
    billing.generate_invoice("s1")  # now open: [2026-01-31, 2026-03-02)
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1:] == [("overage", "Overage: Metered", 150)]


# Plan changes


def test_change_plan_prorates_segments_and_overage(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))  # metered, 33 included
    billing.record_usage("s1", "e2", 250, date(2026, 1, 11))  # pro, 200 included

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 85),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 5185


def test_late_event_goes_to_first_segment(billing, metered):
    billing.generate_invoice("s1")  # open: [2026-01-31, 2026-03-02), 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # metered 10 days, pro 20
    billing.record_usage("s1", "late", 40, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [("overage", "Overage: Metered", 35)]


def test_several_changes_and_half_up_rounding(billing, store):
    store.add_plan(Plan(plan_id="a", name="A", monthly_price_cents=1005))
    store.add_plan(Plan(plan_id="b", name="B", monthly_price_cents=1001))
    billing.change_plan("s1", "a", date(2026, 1, 6))  # basic 5 days
    billing.change_plan("s1", "b", date(2026, 1, 21))  # a 15 days, b 10 days

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 500),
        ("plan", "Plan: A", 503),  # 502.5 rounds up
        ("plan", "Plan: B", 334),  # 333.67
    ]


def test_changing_back_to_an_earlier_plan_is_allowed(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1000, 2000, 1000]


def test_subscription_after_invoice_has_final_plan_and_no_changes(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_raises(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert metered.get_plan_changes("s1") == []


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_not_after_previous_change_raises(billing, metered, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", effective_on)
    assert len(metered.get_plan_changes("s1")) == 1


def test_change_to_current_plan_raises(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription_raises_key_error(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert metered.get_plan_changes("s1") == []
    assert metered.get_subscription("s1").plan_id == "metered"


def test_rejected_change_leaves_invoice_unchanged(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


# Discount, credit and tax with usage


def test_adjustments_apply_to_plan_and_overage_subtotal(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 250, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 5185, discount 518.5 -> 519, credit 500, tax on 4166 -> 416.6 -> 417
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", 85),
        ("overage", 100),
        ("discount", -519),
        ("credit", -500),
        ("tax", 417),
    ]
    assert invoice.total_cents == 4583
    assert customer.credit_balance_cents == 0
    assert metered.get_invoice(invoice.invoice_id) == invoice


def test_unknown_discount_code_leaves_usage_and_changes_unbilled(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="BOGUS")

    invoice = billing.generate_invoice("s1")

    assert invoice.total_cents == 5085
