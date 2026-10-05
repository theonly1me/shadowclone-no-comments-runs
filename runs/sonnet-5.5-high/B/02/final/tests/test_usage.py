from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


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
    store.add_plan(Plan(plan_id="free", name="Free", monthly_price_cents=0))


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False
    assert billing.record_usage("s1", "e2", 5, date(2026, 1, 6)) is True


def test_same_event_id_on_other_subscription_is_distinct(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 5, date(2026, 1, 5)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 10, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
    ]
    assert invoice.total_cents == 3050


def test_usage_within_included_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_is_billed_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert "overage" in [item.kind for item in first.line_items]
    assert [item.kind for item in second.line_items] == ["plan"]


def test_future_event_waits_for_its_period(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert kinds(second)[1] == ("overage", "Overage: Basic", 100)


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_event_on_last_day_is_billed(billing):
    billing.record_usage("s1", "e1", 31, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 10)


def test_change_plan_splits_the_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_plan_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1500, 23]


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_usage_is_attributed_to_segments(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 50),
    ]


def test_included_units_are_prorated_by_floor(billing, store):
    store.add_plan(
        Plan(
            plan_id="tiny",
            name="Tiny",
            monthly_price_cents=0,
            included_units=10,
            overage_unit_price_cents=7,
        )
    )
    billing.change_plan("s1", "tiny", date(2026, 1, 21))
    billing.record_usage("s1", "a", 4, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[2:] == [("overage", "Overage: Tiny", 7)]


def test_overage_lines_come_after_all_plan_lines(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == [
        "plan",
        "plan",
        "overage",
    ]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 40, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
        ("discount", "Discount", -310),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 229),
    ]
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0


def test_change_plan_rejections_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    expected = list(store.get_subscription("s1").plan_changes)

    bad = [
        ("basic", date(2026, 1, 1)),
        ("basic", date(2026, 1, 31)),
        ("basic", date(2026, 2, 5)),
        ("basic", date(2026, 1, 11)),
        ("basic", date(2026, 1, 5)),
        ("pro", date(2026, 1, 20)),
    ]
    for plan_id, effective_on in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert store.get_subscription("s1").plan_changes == expected


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_changes_are_cleared_for_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", "Plan: Pro", 6000)]
