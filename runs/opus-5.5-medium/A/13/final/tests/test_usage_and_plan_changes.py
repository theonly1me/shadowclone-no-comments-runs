from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def metered(store):
    # Period in conftest is 2026-01-01 .. 2026-01-31: 30 days.
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
            monthly_price_cents=9000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    return store


def kinds_and_amounts(invoice):
    return [(item.kind, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# Usage events


def test_record_usage_returns_true_then_false_for_duplicate(billing, metered):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_when_billing(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 3000), ("overage", 250)]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, metered, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # Nothing was stored, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_adds_no_overage(billing, metered):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 3000)]


def test_event_on_period_end_waits_for_next_invoice(billing, metered):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 30))  # last day, in period
    billing.record_usage("s1", "e2", 150, date(2026, 1, 31))  # period_end, next period

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds_and_amounts(first) == [("plan", 3000), ("overage", 5)]
    assert kinds_and_amounts(second) == [("plan", 3000), ("overage", 250)]


def test_events_are_billed_only_once(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds_and_amounts(second) == [("plan", 3000)]


def test_late_event_is_billed_in_open_period(billing, metered):
    billing.generate_invoice("s1")  # closes January
    billing.record_usage("s1", "late", 120, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert kinds_and_amounts(invoice) == [("plan", 3000), ("overage", 100)]


# Plan changes


def test_mid_cycle_change_prorates_plan_lines(billing, metered, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days metered, 20 pro

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
    ]
    assert kinds_and_amounts(invoice) == [("plan", 1000), ("plan", 6000)]
    assert invoice.total_cents == 7000


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    # basic 3000 * 1/30 = 100; odd 45 * 29/30 = 43.5 -> 44
    billing.change_plan("s1", "odd", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 100), ("plan", 44)]


def test_usage_is_attributed_to_segments_and_allowances_are_prorated(
    billing, metered
):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered segment: 10 days, included 100*10//30 = 33.
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    # Pro segment: 20 days, included 300*20//30 = 200.
    billing.record_usage("s1", "b", 210, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert [(i.kind, i.description, i.amount_cents) for i in invoice.line_items] == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Metered", 7 * 5),
        ("overage", "Overage: Pro", 10 * 2),
    ]


def test_included_units_use_floor_division(billing, metered):
    # 7 days of 30: 100*7/30 = 23.33 -> 23 included.
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.record_usage("s1", "a", 24, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert ("overage", 5) in kinds_and_amounts(invoice)


def test_late_events_go_to_first_segment(billing, metered):
    billing.generate_invoice("s1")  # period now 2026-01-31 .. 2026-03-02
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 100, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    # Metered segment: 10 of 30 days -> 33 included, 67 over at 5c.
    assert [i for i in invoice.line_items if i.kind == "overage"][0].description == (
        "Overage: Metered"
    )
    assert ("overage", 67 * 5) in kinds_and_amounts(invoice)


def test_multiple_changes_in_one_period(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [
        ("plan", 1000),
        ("plan", 3000),
        ("plan", 1000),
    ]


def test_plan_after_invoice_is_last_plan_and_changes_cleared(
    billing, metered, store
):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert kinds_and_amounts(invoice) == [("plan", 9000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


@pytest.mark.parametrize("effective_on", [date(2026, 1, 5), date(2026, 1, 10)])
def test_change_not_after_previous_change_is_rejected(
    billing, metered, store, effective_on
):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", effective_on)

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_is_rejected(billing, metered, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    assert subscription.plan_changes == []
    assert subscription.plan_id == "metered"


def test_change_unknown_subscription(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))


def test_rejected_change_leaves_invoice_unchanged(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert kinds_and_amounts(invoice) == [("plan", 3000)]


# Adjustments apply to the full subtotal


def test_discount_credit_and_tax_apply_after_usage(billing, metered, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 1000
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 40, date(2026, 1, 5))  # 7 over at 5c = 35

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1000 + 6000 + 35 = 7035; discount 704 (703.5 rounds up);
    # credit 1000; tax 10% of 5331 = 533.1 -> 533
    assert kinds_and_amounts(invoice) == [
        ("plan", 1000),
        ("plan", 6000),
        ("overage", 35),
        ("discount", -704),
        ("credit", -1000),
        ("tax", 533),
    ]
    assert invoice.total_cents == 5864
    assert customer.credit_balance_cents == 0
