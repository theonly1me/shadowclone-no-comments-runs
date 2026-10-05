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
            monthly_price_cents=1000,
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


def test_plan_defaults_have_no_usage_allowance():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# record_usage


def test_record_usage_returns_true_then_false_for_duplicate(billing, plans):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_when_billing(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 10_000, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store, plans):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, plans, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_has_no_overage_line(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 1000)]


def test_usage_on_or_after_period_end_waits_for_next_invoice(billing, plans):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 100) in lines(second)


def test_usage_is_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 1250
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, plans):
    billing.generate_invoice("s1")  # now [2026-01-31, 2026-03-02)
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 150),
    ]


# change_plan


def test_mid_cycle_upgrade_prorates_both_plans(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days old, 20 days new

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 333),
        ("plan", "Plan: Pro", 6000),
    ]
    assert invoice.total_cents == 6333
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_proration_rounds_half_up(billing, store, plans):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "metered", date(2026, 1, 16))  # 15 / 30 of 1 cent

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Odd", 1)


def test_usage_split_across_segments_with_prorated_allowance(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Metered segment: 10 days, included 100*10//30 = 33.
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    # Late event goes into the first segment too.
    billing.record_usage("s1", "b", 3, date(2025, 12, 20))
    # Pro segment: 20 days, included 300*20//30 = 200.
    billing.record_usage("s1", "c", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 333),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Metered", 50),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 6483


def test_multiple_changes_and_return_to_original_plan(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 333),
        ("plan", "Plan: Pro", 3000),
        ("plan", "Plan: Metered", 333),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_next_period_after_change_bills_new_plan_in_full(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 9000)]


def test_discount_credit_and_tax_apply_after_overage(billing, store, plans):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 100
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 1000),
        ("overage", 500),
        ("discount", -150),
        ("credit", -100),
        ("tax", 125),
    ]
    assert invoice.total_cents == 1375
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, plans, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_must_be_after_previous_change(billing, store, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    for effective_on in (date(2026, 1, 11), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "metered", effective_on)

    assert [c.plan_id for c in store.get_subscription("s1").plan_changes] == ["pro"]


def test_change_to_current_plan_is_rejected(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, store, plans):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []
