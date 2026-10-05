from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# The conftest subscription "s1" runs [2026-01-01, 2026-01-31): 30 days on "basic".


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
    store.add_plan(Plan(plan_id="cheap", name="Cheap", monthly_price_cents=1000))
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_has_usage_fields_defaulting_to_zero():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=1)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# --- record_usage ---------------------------------------------------------


def test_record_usage_returns_true_then_false_for_duplicates(billing, plans):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")
    assert invoice.total_cents == 3000  # 10 units, all included


def test_duplicate_is_ignored_even_with_different_units(billing, plans):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 20 * 5)


def test_event_ids_are_scoped_per_subscription(billing, plans):
    from ledger.models import Subscription

    plans.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_event_id_stays_used_after_it_is_billed(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_raise(billing, plans, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


# --- usage billing --------------------------------------------------------


def test_overage_beyond_included_units(billing, plans):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 70, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 30 * 5),
    ]
    assert invoice.total_cents == 3150


def test_usage_exactly_at_included_has_no_overage_line(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")
    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_on_period_end_waits_for_next_invoice(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    assert [item.kind for item in first.line_items] == ["plan"]

    second = billing.generate_invoice("s1")
    assert lines(second)[-1] == ("overage", "Overage: Metered", 50 * 5)


def test_events_are_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 3250
    assert second.total_cents == 3000


def test_late_event_is_billed_on_the_open_period(billing, plans):
    billing.generate_invoice("s1")  # closes January
    billing.record_usage("s1", "late", 130, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")
    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 30 * 5)


# --- change_plan ----------------------------------------------------------


def test_mid_cycle_upgrade_prorates_plan_and_included_units(billing, plans):
    # 10 days on Metered, 20 days on Pro.
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 50, date(2026, 1, 10))  # Metered, includes 33
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))  # Pro, includes 200

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 17 * 5),
        ("overage", "Overage: Pro", 50 * 2),
    ]
    assert invoice.total_cents == 5185


def test_several_changes_in_one_period_with_rounding(billing, plans):
    # Segments of 7, 7 and 16 days.
    billing.change_plan("s1", "pro", date(2026, 1, 8))
    billing.change_plan("s1", "cheap", date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 700),  # 3000 * 7 / 30
        ("plan", "Plan: Pro", 1400),  # 6000 * 7 / 30
        ("plan", "Plan: Cheap", 533),  # 1000 * 16 / 30 = 533.33
    ]


def test_proration_rounds_half_up(billing, plans):
    plans.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    plans.get_subscription("s1").plan_id = "odd"
    # 15 / 30 of 1001 = 500.5 for both halves.
    billing.change_plan("s1", "cheap", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[0] == ("plan", "Plan: Odd", 501)


def test_late_event_attributed_to_first_segment(billing, plans):
    billing.generate_invoice("s1")  # now [01-31, 03-02), 30 days on Metered
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # 10 days Metered
    billing.record_usage("s1", "late", 40, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")
    assert ("overage", "Overage: Metered", (40 - 33) * 5) in lines(invoice)


def test_plan_id_updates_and_changes_clear_after_invoice(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = plans.get_subscription("s1")

    billing.generate_invoice("s1")

    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    second = billing.generate_invoice("s1")
    assert lines(second) == [("plan", "Plan: Pro", 6000)]


def test_discount_credit_and_tax_follow_overage(billing, plans):
    plans.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = plans.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 3000 + 500 = 3500
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
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
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 1)],
)
def test_change_outside_open_period_raises(billing, plans, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert plans.get_subscription("s1").plan_changes == []


def test_change_must_be_after_previous_change(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "cheap", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "cheap", date(2026, 1, 5))
    assert len(plans.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_raises(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    # Switching back to the original plan is allowed.
    billing.change_plan("s1", "metered", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription_raises(billing, plans):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert plans.get_subscription("s1").plan_changes == []
    assert plans.get_subscription("s1").plan_id == "metered"
