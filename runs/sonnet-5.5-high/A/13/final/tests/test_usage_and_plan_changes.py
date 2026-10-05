from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


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


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 50 * 10)


def test_event_id_is_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5))


def test_duplicate_id_still_rejected_after_billing(billing):
    billing.record_usage("s1", "e1", 1, date(2026, 1, 5))
    billing.generate_invoice("s1")
    assert billing.record_usage("s1", "e1", 1, date(2026, 2, 5)) is False


def test_usage_within_allowance_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_billed_later_and_only_once(billing):
    billing.record_usage("s1", "e1", 200, date(2026, 1, 31))  # == period_end
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]
    second = billing.generate_invoice("s1")
    assert kinds(second)[1] == ("overage", "Overage: Basic", 100 * 10)
    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # period now 01-31 .. 03-02
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 30 * 10)


def test_change_plan_splits_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1001))
    store.add_plan(Plan("other", "Other", 1))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "other", date(2026, 1, 16))  # 15/30 of 1001 = 500.5
    invoice = billing.generate_invoice("s1")
    assert invoice.line_items[0].amount_cents == 501


def test_multiple_changes_and_segment_overage(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "a", 20, date(2026, 1, 1))  # basic seg 1 (10d)
    billing.record_usage("s1", "b", 100, date(2026, 1, 15))  # pro seg (10d)
    billing.record_usage("s1", "c", 50, date(2026, 1, 25))  # basic seg 3 (10d)
    billing.record_usage("s1", "d", 5, date(2025, 12, 1))  # late -> first seg
    invoice = billing.generate_invoice("s1")
    assert [i.amount_cents for i in invoice.line_items[:3]] == [1000, 2000, 1000]
    overages = [i for i in invoice.line_items if i.kind == "overage"]
    # seg1: 25 units <= 33, seg2: 100 <= 100, seg3: 50 - 33 = 17 over
    assert [(o.description, o.amount_cents) for o in overages] == [
        ("Overage: Basic", 170)
    ]
    assert invoice.line_items[-1] is overages[-1]


def test_overage_lines_follow_all_plan_lines(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 100, date(2026, 1, 2))  # basic: 50 included
    billing.record_usage("s1", "b", 200, date(2026, 1, 20))  # pro: 150 included
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 50 * 10),
        ("overage", "Overage: Pro", 50 * 5),
    ]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 200, date(2026, 1, 2))  # 100 over = 1000
    invoice = billing.generate_invoice("s1", discount_code="TEN")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 1000),
        ("discount", "Discount", -400),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "plan, day, error",
    [
        ("pro", date(2026, 1, 1), ValueError),  # == period_start
        ("pro", date(2026, 1, 31), ValueError),  # == period_end
        ("pro", date(2026, 2, 5), ValueError),
        ("pro", date(2025, 12, 31), ValueError),
        ("basic", date(2026, 1, 10), ValueError),  # already current
        ("nope", date(2026, 1, 10), KeyError),
    ],
)
def test_rejected_change_leaves_state_unchanged(billing, store, plan, day, error):
    with pytest.raises(error):
        billing.change_plan("s1", plan, day)
    assert store.get_subscription("s1").plan_changes == []
    assert store.get_subscription("s1").plan_id == "basic"


def test_change_ordering_and_same_plan_rules(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for plan, day in [
        ("basic", date(2026, 1, 11)),  # not strictly after previous
        ("basic", date(2026, 1, 5)),  # before previous
        ("pro", date(2026, 1, 20)),  # already current (pro)
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan, day)
    assert store.get_subscription("s1").plan_changes == [(date(2026, 1, 11), "pro")]
    # switching back to the period's starting plan is allowed
    billing.change_plan("s1", "basic", date(2026, 1, 12))


def test_changes_reset_for_next_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")
    # next period 01-31 .. 03-02 is a single Pro segment
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [("plan", "Plan: Pro", 6000)]
    billing.change_plan("s1", "basic", date(2026, 3, 3))  # period is now 03-02..04-01
