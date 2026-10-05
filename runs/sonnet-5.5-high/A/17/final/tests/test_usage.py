from datetime import date

import pytest

from ledger.models import Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=30, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("lite", "Lite", 1500, included_units=0, overage_unit_price_cents=5)
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 20


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_idempotent(billing):
    assert billing.record_usage("s1", "e1", 15, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[1] == ("overage", "Overage: Basic", 5 * 20)
    # still ignored after being billed
    assert billing.record_usage("s1", "e1", 99, date(2026, 2, 6)) is False


def test_overage_billed_once(billing):
    billing.record_usage("s1", "e1", 15, date(2026, 1, 5))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3000


def test_no_overage_within_included(billing):
    billing.record_usage("s1", "e1", 10, date(2026, 1, 5))
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_not_billed_yet(billing):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))
    assert billing.generate_invoice("s1").total_cents == 3000
    assert billing.generate_invoice("s1").total_cents == 3000 + 40 * 20


def test_late_event_billed_on_open_period_first_segment(billing):
    billing.record_usage("s1", "e1", 11, date(2025, 12, 20))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    # basic included = 10*15//30 = 5 -> 6 overage units at 20
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 120),
    ]


def test_change_plan_segments_and_usage_attribution(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro
    billing.record_usage("s1", "a", 4, date(2026, 1, 10))  # basic, included 3
    billing.record_usage("s1", "b", 25, date(2026, 1, 11))  # pro, included 20
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 50),
    ]
    assert invoice.total_cents == 5070
    sub = store.get_subscription("s1")
    assert sub.plan_id == "pro"
    assert sub.plan_changes == []


def test_multiple_changes_and_back_to_original(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 7))
    billing.change_plan("s1", "basic", date(2026, 1, 13))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 600),
        ("plan", "Plan: Pro", 1200),
        ("plan", "Plan: Basic", 1800),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_plan_line_rounding_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1001))
    billing.change_plan("s1", "odd", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    # 3000*15/30 = 1500 ; 1001*15/30 = 500.5 -> 501
    assert [i.amount_cents for i in invoice.line_items] == [1500, 501]


def test_invalid_changes_leave_state_unchanged(billing, store):
    sub = store.get_subscription("s1")
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    bad = [
        ("lite", date(2026, 1, 1)),  # == period_start
        ("lite", date(2026, 1, 31)),  # == period_end
        ("lite", date(2026, 2, 5)),
        ("lite", date(2026, 1, 10)),  # == previous change
        ("lite", date(2026, 1, 9)),  # before previous change
        ("pro", date(2026, 1, 20)),  # already current
    ]
    for plan_id, day in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "lite", date(2026, 1, 20))
    assert sub.plan_changes == [(date(2026, 1, 10), "pro")]
    assert sub.plan_id == "basic"


def test_change_to_current_original_plan_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_discount_credit_tax_apply_to_usage_subtotal(billing, store):
    from decimal import Decimal

    from ledger.models import DiscountCode

    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))  # 10 over -> 200
    invoice = billing.generate_invoice("s1", discount_code="TEN")
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -320),
        ("credit", -500),
        ("tax", 238),
    ]
    assert invoice.total_cents == 2618
    assert customer.credit_balance_cents == 0
