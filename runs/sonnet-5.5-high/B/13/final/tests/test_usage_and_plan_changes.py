from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=60,
            overage_unit_price_cents=5,
        )
    )


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_same_event_id_on_different_subscriptions(billing, store):
    store.add_subscription(
        Subscription(
            subscription_id="s2",
            customer_id="c1",
            plan_id="basic",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 10, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
    ]
    assert invoice.total_cents == 3050


def test_usage_within_included_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_event_is_billed_later_and_only_once(billing):
    billing.record_usage("s1", "future", 40, date(2026, 1, 31))
    billing.record_usage("s1", "now", 31, date(2026, 1, 30))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1] == ("overage", "Overage: Basic", 10)
    assert lines(second)[1] == ("overage", "Overage: Basic", 100)
    assert [item.kind for item in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_recorded_duplicate_after_billing_is_ignored(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 2))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 2, 2)) is False
    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_mid_cycle_change_splits_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=15))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Basic", 1500)
    assert lines(invoice)[1] == ("plan", "Plan: Odd", 8)


def test_usage_is_attributed_per_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 20, date(2026, 1, 11))
    billing.record_usage("s1", "c", 50, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 150),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 20, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2] == ("overage", "Overage: Basic", 100)


def test_included_units_are_prorated_with_floor(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 11, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2] == ("overage", "Overage: Basic", 10)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_invalid_changes_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    for plan_id, effective_on, error in [
        ("basic", date(2026, 1, 1), ValueError),
        ("basic", date(2026, 1, 31), ValueError),
        ("basic", date(2026, 1, 11), ValueError),
        ("basic", date(2026, 1, 5), ValueError),
        ("pro", date(2026, 1, 20), ValueError),
        ("missing", date(2026, 1, 20), KeyError),
    ]:
        with pytest.raises(error):
            billing.change_plan("s1", plan_id, effective_on)
        assert subscription.plan_changes == before
        assert subscription.plan_id == "basic"


def test_change_to_current_plan_without_changes_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_change_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))


def test_changes_do_not_leak_into_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 31))


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 40, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 100),
        ("discount", -310),
        ("credit", -500),
        ("tax", 229),
    ]
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0
