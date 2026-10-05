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
    store.add_plan(Plan(plan_id="team", name="Team", monthly_price_cents=9000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 9)) is False


def test_duplicate_event_is_ignored_completely(billing):
    billing.record_usage("s1", "e1", 31, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 10)


def test_same_event_id_on_other_subscription_is_distinct(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_invalid_event_is_not_recorded(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 25, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 150),
    ]
    assert invoice.total_cents == 3150


def test_event_at_period_end_is_not_billed_yet(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Basic", 700) in lines(second)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan", "overage"]
    assert [item.kind for item in second.line_items] == ["plan"]


def test_billed_event_id_stays_idempotent(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is False
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2025, 12, 15))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 100) in lines(invoice)


def test_change_plan_splits_period(billing, store):
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


def test_plan_change_rounds_half_up_per_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [100, 5800]


def test_multiple_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.change_plan("s1", "basic", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 1500),
        ("plan", "Plan: Basic", 500),
    ]


def test_usage_attributed_to_segments_with_prorated_included(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 30, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
    ]


def test_overage_lines_follow_all_plan_lines_in_order(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 1))
    billing.record_usage("s1", "b", 50, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 50),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 100, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    kinds = [(item.kind, item.description) for item in invoice.line_items]
    assert kinds == [
        ("plan", "Plan: Basic"),
        ("plan", "Plan: Pro"),
        ("overage", "Overage: Basic"),
    ]


def test_included_units_floor_division(billing, store):
    store.add_plan(
        Plan(
            plan_id="small",
            name="Small",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=7,
        )
    )
    store.get_subscription("s1").plan_id = "small"
    billing.change_plan("s1", "pro", date(2026, 1, 6))
    billing.record_usage("s1", "a", 3, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Small", 14) in lines(invoice)


def test_discount_credit_tax_apply_to_subtotal_with_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 80, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
        ("discount", "Discount", -350),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_change_plan_validation(billing, store):
    subscription = store.get_subscription("s1")
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    for bad in (date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5), date(2026, 1, 11), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "team", bad)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))

    assert [(c.effective_on, c.plan_id) for c in subscription.plan_changes] == [
        (date(2026, 1, 11), "pro")
    ]


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_changes_do_not_leak_into_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]
