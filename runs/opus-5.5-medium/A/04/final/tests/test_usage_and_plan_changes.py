from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

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
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.get_subscription("s1").plan_id = "metered"


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# record_usage


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is True
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is False


def test_duplicate_event_is_ignored_even_with_different_data(billing, store, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 3))
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 4)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 50 * 5)


def test_duplicate_detection_survives_invoicing(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 3))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 3)) is False


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 3)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 3))
    # Nothing was recorded, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 3))


# Usage billing


def test_usage_within_allowance_adds_no_overage(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_over_allowance_adds_overage(billing, plans):
    billing.record_usage("s1", "e1", 80, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 20 * 5),
    ]
    assert invoice.total_cents == 3100


def test_event_on_period_end_is_billed_next_period(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Metered", 50 * 5)


def test_events_are_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan", "overage"]
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, plans):
    billing.generate_invoice("s1")  # closes January
    billing.record_usage("s1", "late", 130, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 30 * 5)


def test_failed_invoice_does_not_consume_usage(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 1)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 50 * 5)


# change_plan


def test_mid_cycle_upgrade_prorates_plan_and_allowance(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))  # Metered: 33 included
    billing.record_usage("s1", "e2", 250, date(2026, 1, 11))  # Pro: 200 included

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 17 * 5),
        ("overage", "Overage: Pro", 50 * 2),
    ]
    assert invoice.total_cents == 5185


def test_late_event_is_attributed_to_first_segment(billing, plans):
    billing.generate_invoice("s1")  # open period is now [01-31, 03-02), 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # 10 days on Metered
    billing.record_usage("s1", "late", 40, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", (40 - 33) * 5)


def test_several_changes_in_one_period(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []


def test_proration_rounds_half_up(billing, store, plans):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=2001))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # 2001 * 15 / 30 = 1000.5
    assert lines(invoice)[1] == ("plan", "Plan: Odd", 1001)


def test_new_plan_applies_after_invoice(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_id == "metered"

    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")

    assert store.get_subscription("s1").plan_id == "pro"
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store, plans):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_must_be_strictly_inside_period(billing, store, plans, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_must_follow_previous_change(billing, store, plans, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", effective_on)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, store, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_unknown_plan_or_subscription(billing, store, plans):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []
