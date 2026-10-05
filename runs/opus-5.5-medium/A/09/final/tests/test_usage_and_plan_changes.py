from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# The fixture subscription bills [2026-01-01, 2026-01-31): a 30 day period.


@pytest.fixture(autouse=True)
def usage_plans(store):
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


def test_record_usage_is_idempotent_by_event_id(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1:] == [("overage", "Overage: Metered", 50 * 5)]


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_replayed_event_stays_a_duplicate_after_billing(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 1))

    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Metered", 3000)]


def test_event_on_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert lines(second)[1:] == [("overage", "Overage: Metered", 20 * 5)]


def test_late_event_is_billed_on_the_open_period_once(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 130, date(2026, 1, 15))

    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(second)[1:] == [("overage", "Overage: Metered", 30 * 5)]
    assert [i.kind for i in third.line_items] == ["plan"]


def test_mid_cycle_upgrade_prorates_price_and_allowance(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered segment: 10 days, 33 included. Pro segment: 20 days, 200 included.
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 7 * 5),
        ("overage", "Overage: Pro", 50 * 2),
    ]
    assert invoice.total_cents == 5135
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    store.get_subscription("s1").period_end = date(2026, 1, 3)  # 2 days
    billing.change_plan("s1", "metered", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Odd", 501),
        ("plan", "Plan: Metered", 1500),
    ]


def test_multiple_changes_and_late_events_go_to_first_segment(billing):
    billing.generate_invoice("s1")  # open period is now [01-31, 03-02), 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.change_plan("s1", "metered", date(2026, 2, 20))
    billing.record_usage("s1", "late", 50, date(2026, 1, 2))
    billing.record_usage("s1", "first", 50, date(2026, 2, 1))

    invoice = billing.generate_invoice("s1")

    # Segments: 10 days metered (33 incl), 10 days pro, 10 days metered.
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 67 * 5),
    ]


def test_plan_changes_are_cleared_after_invoice(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]
    # Changes in the new open period, [03-02, 04-01), are accepted again.
    billing.change_plan("s1", "metered", date(2026, 3, 3))


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2026, 1, 31), date(2025, 12, 31)]
)
def test_change_must_be_strictly_inside_period(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))
    assert len(store.get_plan_changes("s1")) == 1


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_plan_changes("s1") == []
    assert store.get_subscription("s1").plan_id == "metered"


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0
