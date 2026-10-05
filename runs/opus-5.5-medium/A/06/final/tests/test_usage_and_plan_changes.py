from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def usage_plans(store):
    # The fixture period is 2026-01-01 to 2026-01-31: 30 days.
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


# Usage events


def test_plan_defaults_have_no_usage_allowance():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_within_allowance_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_over_allowance_is_billed_as_overage(billing):
    billing.record_usage("s1", "e1", 80, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 50, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 150),
    ]
    assert invoice.total_cents == 3150


def test_record_usage_returns_true_for_new_event(billing):
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_duplicate_event_id_is_ignored(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")
    assert ("overage", "Overage: Metered", 250) in lines(invoice)


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -3])
def test_non_positive_units_are_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))


def test_usage_for_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_event_on_period_end_waits_for_the_next_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 250) in lines(second)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 120, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert ("overage", "Overage: Metered", 100) in lines(invoice)


# Plan changes


def test_mid_cycle_upgrade_prorates_both_plans(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    store.get_subscription("s1").plan_id = "odd"
    # 45 * 3 / 30 = 4.5 rounds up to 5.
    billing.change_plan("s1", "basic", date(2026, 1, 4))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Odd", 5),
        ("plan", "Plan: Basic", 2700),
    ]


def test_usage_is_attributed_to_the_segment_it_occurred_in(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered segment: 10 days, includes 100 * 10 // 30 = 33 units.
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))
    # Pro segment: 20 days, includes 300 * 20 // 30 = 200 units.
    billing.record_usage("s1", "e2", 210, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Pro", 20),
    ]


def test_late_event_is_attributed_to_the_first_segment(billing):
    billing.generate_invoice("s1")  # period is now 2026-01-31 to 2026-03-02
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    # Metered segment is 10 of 30 days: 33 included, 17 over at 5 cents.
    assert ("overage", "Overage: Metered", 85) in lines(invoice)
    assert not any(item.description == "Overage: Pro" for item in invoice.line_items)


def test_several_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]


def test_changing_back_to_an_earlier_plan_creates_separate_segments(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))
    billing.record_usage("s1", "e1", 40, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    # Each Metered segment includes 33 units on its own: 7 over in each.
    assert [item for item in lines(invoice) if item[0] == "overage"] == [
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Metered", 35),
    ]


def test_after_invoice_subscription_is_on_final_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    assert store.get_subscription("s1").plan_id == "pro"
    assert store.list_plan_changes("s1") == []

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_changes_are_allowed_again_in_the_next_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 21))
    billing.generate_invoice("s1")

    billing.change_plan("s1", "metered", date(2026, 2, 1))

    assert len(store.list_plan_changes("s1")) == 1


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_the_open_period_is_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.list_plan_changes("s1") == []
    assert store.get_subscription("s1").plan_id == "metered"


@pytest.mark.parametrize("effective_on", [date(2026, 1, 5), date(2026, 1, 11)])
def test_change_not_after_previous_change_is_rejected(billing, store, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", effective_on)
    assert len(store.list_plan_changes("s1")) == 1


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert len(store.list_plan_changes("s1")) == 1


def test_change_to_unknown_plan_raises_key_error(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    assert store.list_plan_changes("s1") == []


def test_change_for_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_rejected_change_leaves_invoice_unchanged(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


# Adjustments on top of usage


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", 35),
        ("discount", -504),  # 10% of 5035, half up
        ("credit", -500),
        ("tax", 403),  # 10% of 4031, half up
    ]
    assert invoice.total_cents == 4434
    assert customer.credit_balance_cents == 0
