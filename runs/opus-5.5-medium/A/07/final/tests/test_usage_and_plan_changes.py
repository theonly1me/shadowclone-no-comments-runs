from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    # Period in the shared fixture is [2026-01-01, 2026-01-31): 30 days.
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


def test_plan_defaults_have_no_usage_component():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# Usage events


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_on_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 9999, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 250) in lines(invoice)


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # The rejected call did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_usage(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_overage_is_billed_after_plan_line(billing):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 60, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]
    assert invoice.total_cents == 3100


def test_event_on_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 250) in lines(second)


def test_events_are_billed_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in second.line_items] == ["plan"]
    # The id stays known after billing.
    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert ("overage", "Overage: Metered", 150) in lines(invoice)


# Plan changes


def test_mid_cycle_change_prorates_plans_and_allowance(billing, store):
    # 10 days of Metered, 20 days of Pro.
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # Metered allows 33
    billing.record_usage("s1", "b", 210, date(2026, 1, 11))  # Pro allows 200

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Pro", 20),
    ]
    assert invoice.total_cents == 5055
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    # 15 / 30 of 1001 = 500.5 -> 501, and the same for the second half.
    billing.change_plan("s1", "metered", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Odd", 501)
    assert lines(invoice)[1] == ("plan", "Plan: Metered", 1500)


def test_multiple_changes_including_back_to_earlier_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))
    billing.record_usage("s1", "late", 50, date(2025, 12, 20))  # first segment
    billing.record_usage("s1", "x", 40, date(2026, 1, 25))  # third segment

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 85),
        ("overage", "Overage: Metered", 35),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_changes_are_cleared_after_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_rejected(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_must_be_after_previous_change(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))


def test_change_to_current_plan_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_unknown_plan_or_subscription_rejected(billing):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_rejected_change_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 31))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
    ]


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))  # 200 over -> 1000

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
