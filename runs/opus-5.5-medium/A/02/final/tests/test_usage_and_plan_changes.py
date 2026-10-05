from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# The fixture period is [2026-01-01, 2026-01-31): 30 days.


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


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# --- record_usage -----------------------------------------------------------


def test_record_usage_returns_true_then_false_for_duplicate(billing, metered):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    # Only the first call counts: 50 units over the 100 included.
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_duplicate_is_ignored_even_after_billing(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False
    invoice = billing.generate_invoice("s1")
    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_ids_are_scoped_per_subscription(billing, metered):
    from ledger.models import Subscription

    metered.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, metered, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # Nothing was recorded, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage_line(billing, metered):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_on_period_end_waits_for_next_invoice(billing, metered):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 100) in lines(second)


def test_late_event_is_billed_on_open_period_and_only_once(billing, metered):
    billing.generate_invoice("s1")  # now open: [2026-01-31, 2026-03-02)
    billing.record_usage("s1", "late", 110, date(2026, 1, 15))

    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 50) in lines(second)
    assert [item.kind for item in third.line_items] == ["plan"]


# --- change_plan ------------------------------------------------------------


def test_mid_cycle_upgrade_prorates_and_splits_usage(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))  # metered segment
    billing.record_usage("s1", "e2", 210, date(2026, 1, 11))  # pro segment
    billing.record_usage("s1", "late", 10, date(2025, 12, 20))  # first segment

    invoice = billing.generate_invoice("s1")

    # Metered: 10 days, included 100*10//30 = 33, used 50 -> 17 over at 5.
    # Pro: 20 days, included 300*20//30 = 200, used 210 -> 10 over at 2.
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 85),
        ("overage", "Overage: Pro", 20),
    ]
    assert invoice.total_cents == 5105


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.add_plan(Plan(plan_id="odd2", name="Odd2", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "odd2", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [501, 501]


def test_several_changes_in_one_period(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.change_plan("s1", "metered", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 500),
        ("plan", "Plan: Metered", 500),
    ]


def test_after_invoice_plan_is_final_and_changes_are_cleared(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_plan_id_is_unchanged_until_invoice(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    assert metered.get_subscription("s1").plan_id == "metered"


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 1)],
)
def test_change_must_be_strictly_inside_period(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_must_be_after_previous_change(billing, metered, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", effective_on)


def test_change_to_current_plan_is_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))


def test_rejected_change_leaves_state_unchanged(billing, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
    ]


# --- adjustments ------------------------------------------------------------


def test_discount_credit_and_tax_apply_to_plan_plus_overage(billing, metered):
    metered.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = metered.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))  # 200 over at 5

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # Subtotal 4000, discount 400, credit 500, tax 10% of 3100.
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_unknown_discount_code_does_not_consume_usage_or_changes(billing, metered):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 585) in lines(invoice)
    assert ("plan", "Plan: Pro", 4000) in lines(invoice)
