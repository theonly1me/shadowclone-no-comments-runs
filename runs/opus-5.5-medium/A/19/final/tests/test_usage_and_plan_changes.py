from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


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
            monthly_price_cents=9000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.get_subscription("s1").plan_id = "metered"


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


# Usage events


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_on_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]
    assert invoice.total_cents == 3250


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_raise(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_event_waits_for_its_period_and_is_billed_once(billing):
    billing.record_usage("s1", "e1", 101, date(2026, 1, 30))
    billing.record_usage("s1", "e2", 110, date(2026, 1, 31))  # == period_end

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1:] == [("overage", "Overage: Metered", 5)]
    assert lines(second)[1:] == [("overage", "Overage: Metered", 50)]
    assert lines(third)[1:] == []


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")  # period is now Jan 31 - Mar 2
    billing.record_usage("s1", "late", 120, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[1:] == [("overage", "Overage: Metered", 100)]


# Plan changes


def test_mid_cycle_upgrade_prorates_both_plans(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days old, 20 days new

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 6000),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1001))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15 / 30 days each

    invoice = billing.generate_invoice("s1")

    # 1001 * 15 / 30 = 500.5 -> 501
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1500),
        ("plan", "Plan: Odd", 501),
    ]


def test_usage_is_split_by_segment_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered segment: 10 days, included 100*10//30 = 33.
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))
    # Late event goes to the first segment.
    billing.record_usage("s1", "late", 3, date(2025, 12, 20))
    # Pro segment: 20 days, included 300*20//30 = 200.
    billing.record_usage("s1", "e2", 210, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Metered", 50),  # (43 - 33) * 5
        ("overage", "Overage: Pro", 20),  # (210 - 200) * 2
    ]
    assert invoice.total_cents == 7070


def test_several_changes_and_back_to_original_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 7))
    billing.change_plan("s1", "metered", date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 600),
        ("plan", "Plan: Pro", 5400),
        ("plan", "Plan: Metered", 600),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_next_period_bills_new_plan_in_full(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 9000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_raises(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_not_after_previous_change_raises(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    for effective_on in (date(2026, 1, 10), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "basic", effective_on)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_raises(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_unknown_plan_or_subscription_raises_key_error(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 43, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("plan", 6000),
        ("overage", 50),
        ("discount", -705),
        ("credit", -500),
        ("tax", 585),  # (7050 - 705 - 500) * 10% = 584.5 -> 585
    ]
    assert invoice.total_cents == 6430
    assert customer.credit_balance_cents == 0
