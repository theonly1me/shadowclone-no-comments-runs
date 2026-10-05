from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def usage_plans(store):
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


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 9, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_completely(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 2, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_same_event_id_on_different_subscriptions_is_independent(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_overage_is_billed(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 10, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
    ]
    assert invoice.total_cents == 3050


def test_event_on_period_end_is_billed_next_period(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert [item.kind for item in second.line_items] == ["plan", "overage"]


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan", "overage"]
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period_in_first_segment(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 2, 10))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == [
        "plan",
        "plan",
        "overage",
    ]
    assert invoice.line_items[2].description == "Overage: Basic"


def test_change_plan_prorates_segments(billing, store):
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


def test_plan_rounding_half_up_per_segment(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1))
    store.get_plan("basic").monthly_price_cents = 1
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1, 1]


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_usage_attributed_to_segment_and_included_units_prorated(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 50),
    ]


def test_overage_lines_follow_all_plan_lines_then_discount_credit_tax(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("discount", "Discount", -510),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 409),
    ]
    assert invoice.total_cents == sum(item.amount_cents for item in invoice.line_items)
    assert customer.credit_balance_cents == 0


def test_changes_do_not_carry_into_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 6000)
    ]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_plan_outside_period_is_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_plan_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 21))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_plan_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []
