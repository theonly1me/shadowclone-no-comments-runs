from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

D = date


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=5)
    )


def kinds(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_idempotent(billing, store):
    assert billing.record_usage("s1", "e1", 150, D(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, D(2026, 1, 6)) is False
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
    ]


def test_same_event_id_other_subscription_is_distinct(billing):
    assert billing.record_usage("s1", "e1", 1, D(2026, 1, 5))


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, D(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, D(2026, 1, 5))
    # The rejected call did not consume the id.
    assert billing.record_usage("s1", "e1", 1, D(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, D(2026, 1, 5))
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_billed_later_and_once(billing):
    billing.record_usage("s1", "e1", 200, D(2026, 1, 31))  # == period_end
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]
    second = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 1000) in kinds(second)
    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 110, D(2025, 12, 1))
    invoice = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 100) in kinds(invoice)


def test_plan_change_splits_period(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))  # 10 days basic, 20 pro
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    sub = store.get_subscription("s1")
    assert sub.plan_id == "pro"
    assert sub.plan_changes == []


def test_plan_change_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 5))
    billing.change_plan("s1", "odd", D(2026, 1, 16))  # 15/30 of 3000 and 5
    invoice = billing.generate_invoice("s1")
    assert [i.amount_cents for i in invoice.line_items] == [1500, 3]  # 2.5 -> 3


def test_usage_attributed_per_segment(billing):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    # basic: 10 days -> included 33; pro: 20 days -> included 200
    billing.record_usage("s1", "a", 40, D(2026, 1, 10))
    billing.record_usage("s1", "b", 250, D(2026, 1, 11))
    billing.record_usage("s1", "c", 5, D(2025, 12, 20))  # late -> first segment
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 120),  # 45 - 33 = 12 units * 10
        ("overage", "Overage: Pro", 250),  # 50 * 5
    ]


def test_multiple_changes_and_back(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.change_plan("s1", "basic", D(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]
    assert invoice.total_cents == 1000 + 2000 + 1000
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "plan, day, error",
    [
        ("pro", D(2026, 1, 1), ValueError),  # == period_start
        ("pro", D(2026, 1, 31), ValueError),  # == period_end
        ("pro", D(2026, 2, 5), ValueError),
        ("basic", D(2026, 1, 10), ValueError),  # already current
        ("ghost", D(2026, 1, 10), KeyError),
    ],
)
def test_rejected_change_leaves_state(billing, store, plan, day, error):
    with pytest.raises(error):
        billing.change_plan("s1", plan, day)
    assert store.get_subscription("s1").plan_changes == []


def test_changes_must_be_increasing_and_not_same_plan(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    for plan, day in [("basic", D(2026, 1, 11)), ("basic", D(2026, 1, 5)), ("pro", D(2026, 1, 20))]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan, day)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_changes_cleared_after_invoice(billing):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [("plan", "Plan: Pro", 6000)]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 150, D(2026, 1, 5))  # 500 overage
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_failed_invoice_leaves_state(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.record_usage("s1", "e1", 500, D(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "NOPE")
    sub = store.get_subscription("s1")
    assert sub.period_start == D(2026, 1, 1) and len(sub.plan_changes) == 1
    assert not store.get_usage_events("s1")[0].billed
