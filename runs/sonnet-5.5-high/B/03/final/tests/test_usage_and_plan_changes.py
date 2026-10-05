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
            monthly_price_cents=9000,
            included_units=90,
            overage_unit_price_cents=5,
        )
    )


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 3)) is False

    billing.record_usage("s1", "e2", 26, date(2026, 1, 4))
    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 10),
    ]


def test_same_event_id_on_different_subscriptions_is_independent(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 2)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 2))

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True


def test_usage_within_allowance_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_event_is_billed_on_a_later_invoice(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert kinds(second)[1] == ("overage", "Overage: Basic", 100)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 2))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds(first)[1] == ("overage", "Overage: Basic", 100)
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_mid_period_change_splits_plan_lines(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
    ]
    assert invoice.total_cents == 7000


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1500, 1]


def test_usage_is_attributed_to_segments_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 100, date(2026, 1, 11))
    billing.record_usage("s1", "c", 70, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 5 * (170 - 60)),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 20, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    overage = [item for item in invoice.line_items if item.kind == "overage"]
    assert [(i.description, i.amount_cents) for i in overage] == [
        ("Overage: Basic", 10 * (20 - 10))
    ]


def test_included_units_floor_division(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 2))
    billing.record_usage("s1", "a", 4, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    overage = [item for item in invoice.line_items if item.kind == "overage"]
    assert [(i.description, i.amount_cents) for i in overage] == [
        ("Overage: Basic", 30)
    ]


def test_multiple_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 3000),
        ("plan", "Plan: Basic", 1000),
    ]


def test_change_validation_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    bad = [
        ("pro", date(2026, 1, 20)),
        ("basic", date(2026, 1, 11)),
        ("basic", date(2026, 1, 10)),
        ("basic", date(2026, 1, 1)),
        ("basic", date(2026, 1, 31)),
        ("basic", date(2026, 2, 5)),
    ]
    for plan_id, day in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected_before_any_change(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_state_after_invoice(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)

    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [("plan", "Plan: Pro", 9000)]


def test_change_in_next_period_is_validated_against_new_period(billing):
    billing.generate_invoice("s1")

    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))
    billing.change_plan("s1", "pro", date(2026, 2, 1))


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 900),
        ("discount", "Discount", -790),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 661),
    ]
    assert invoice.total_cents == sum(item.amount_cents for item in invoice.line_items)
    assert customer.credit_balance_cents == 0


def test_unknown_discount_code_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2026, 1, 5))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    subscription = store.get_subscription("s1")
    assert len(subscription.plan_changes) == 1
    assert subscription.period_start == date(2026, 1, 1)
    invoice = billing.generate_invoice("s1")
    assert any(item.kind == "overage" for item in invoice.line_items)
