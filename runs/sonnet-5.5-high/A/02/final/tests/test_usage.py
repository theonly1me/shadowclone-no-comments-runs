from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=30, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=5)
    )


def kinds(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent_per_subscription(billing, store):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_event_id_stays_deduplicated_after_billing(billing):
    billing.record_usage("s1", "e1", 1, date(2026, 1, 5))
    billing.generate_invoice("s1")
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is False


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1")
    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_future_event_is_billed_later_and_once(billing):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))  # == period_end
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]
    second = billing.generate_invoice("s1")
    assert kinds(second)[1] == ("overage", "Overage: Basic", 200)
    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_billed_in_open_period_first_segment(billing):
    billing.generate_invoice("s1")  # period is now Jan 31 - Mar 2
    billing.record_usage("s1", "late", 31, date(2026, 1, 10))
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 10)


def test_plan_change_splits_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_subscription("s1").plan_changes == []


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1))
    store.add_plan(Plan("odd2", "Odd2", 1))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "odd2", date(2026, 1, 16))  # 15/30 of 1 cent
    invoice = billing.generate_invoice("s1")
    assert [i.amount_cents for i in invoice.line_items] == [1, 1]


def test_usage_attributed_by_segment_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic: 10 days -> included 30*10//30 = 10; pro: 20 days -> 60*20//30 = 40
    billing.record_usage("s1", "a", 15, date(2026, 1, 10))  # basic
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))  # pro (effective day)
    billing.record_usage("s1", "c", 1, date(2025, 12, 1))  # late -> first segment
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 60),
        ("overage", "Overage: Pro", 50),
    ]


def test_multiple_changes_back_to_original_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "plan_id, day, error",
    [
        ("pro", date(2026, 1, 1), ValueError),  # == period_start
        ("pro", date(2026, 1, 31), ValueError),  # == period_end
        ("pro", date(2026, 2, 5), ValueError),
        ("basic", date(2026, 1, 10), ValueError),  # already current
        ("nope", date(2026, 1, 10), KeyError),
    ],
)
def test_invalid_change_is_rejected_without_side_effects(billing, store, plan_id, day, error):
    with pytest.raises(error):
        billing.change_plan("s1", plan_id, day)
    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for plan_id, day in [("basic", date(2026, 1, 11)), ("basic", date(2026, 1, 5)),
                         ("pro", date(2026, 1, 20))]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))  # 20 over -> 200
    invoice = billing.generate_invoice("s1", discount_code="TEN")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 200),
        ("discount", "Discount", -320),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 238),
    ]
    assert invoice.total_cents == 3000 + 200 - 320 - 500 + 238
    assert customer.credit_balance_cents == 0
