from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# The fixture period is [2026-01-01, 2026-01-31): 30 days.


@pytest.fixture
def store(store):
    basic = store.get_plan("basic")
    basic.included_units = 100
    basic.overage_unit_price_cents = 5
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="cheap", name="Cheap", monthly_price_cents=15))
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


# --- record_usage ---------------------------------------------------------


def test_record_usage_is_idempotent_by_event_id(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 250) in lines(invoice)


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not reserve the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Basic", 3000)]


def test_event_on_or_after_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert ("overage", "Overage: Basic", 5) in lines(second)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 5) in lines(first)
    assert [i.kind for i in second.line_items] == ["plan"]
    # The id stays taken after billing.
    assert billing.record_usage("s1", "e1", 5, date(2026, 2, 5)) is False


def test_late_event_is_billed_on_open_period_in_first_segment(billing):
    billing.generate_invoice("s1")  # now [2026-01-31, 2026-03-02)
    billing.record_usage("s1", "late", 50, date(2026, 1, 10))
    billing.record_usage("s1", "now", 60, date(2026, 2, 20))
    billing.change_plan("s1", "pro", date(2026, 2, 10))

    invoice = billing.generate_invoice("s1")

    # Basic segment: 10 days, includes 100*10//30 = 33; 50 late units -> 17 over.
    # Pro segment: 20 days, includes 200; 60 units -> none over.
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 85),
    ]


# --- change_plan ----------------------------------------------------------


def test_mid_cycle_change_prorates_and_splits_usage(billing, store):
    billing.record_usage("s1", "a", 40, date(2026, 1, 1))
    billing.record_usage("s1", "b", 10, date(2026, 1, 10))
    billing.record_usage("s1", "c", 250, date(2026, 1, 11))  # first Pro day
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    # Basic: 10 days, includes 33, used 50 -> 17 * 5.
    # Pro: 20 days, includes 200, used 250 -> 50 * 2.
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 85),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 5185

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)


def test_plan_id_unchanged_until_invoice_then_full_new_price(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_id == "basic"

    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_several_changes_and_half_up_rounding(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    billing.change_plan("s1", "cheap", date(2026, 1, 3))
    billing.change_plan("s1", "basic", date(2026, 1, 4))

    invoice = billing.generate_invoice("s1")

    # 3000*1/30, 6000*1/30, 15*1/30 = 0.5 -> 1, 3000*27/30.
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 100),
        ("plan", "Plan: Pro", 200),
        ("plan", "Plan: Cheap", 1),
        ("plan", "Plan: Basic", 2700),
    ]


def test_proration_rounds_half_up_not_to_even(billing):
    billing.change_plan("s1", "cheap", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    # 15 * 29 / 30 = 14.5 -> 15.
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 100),
        ("plan", "Plan: Cheap", 15),
    ]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    for effective_on in (date(2026, 1, 10), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "cheap", effective_on)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    # Switching back to the original plan is allowed.
    billing.change_plan("s1", "basic", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


# --- adjustments ----------------------------------------------------------


def test_discount_credit_and_tax_apply_to_plan_plus_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))  # 200 over -> 1000

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


def test_unknown_discount_code_does_not_consume_usage(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 5) in lines(invoice)
