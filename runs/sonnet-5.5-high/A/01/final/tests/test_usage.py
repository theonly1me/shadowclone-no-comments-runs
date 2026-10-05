from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    # Period is [Jan 1, Jan 31): 30 days.
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=5)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")
    assert [i.kind for i in invoice.line_items] == ["plan"]  # 5 units < included


def test_duplicate_event_ignored_even_with_different_values(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 2))
    billing.record_usage("s1", "e1", 50, date(2026, 1, 3))
    invoice = billing.generate_invoice("s1")
    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_same_event_id_on_other_subscription_is_distinct(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 2)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 2))
    # The rejected call must not have consumed the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True


def test_overage_line_after_plan_line(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
    ]
    assert invoice.total_cents == 3500


def test_usage_at_included_limit_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_event_on_period_end_is_billed_next_period_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]
    second = billing.generate_invoice("s1")
    assert lines(second)[1] == ("overage", "Overage: Basic", 500)
    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # period is now [Jan 31, Mar 2)
    billing.record_usage("s1", "late", 150, date(2026, 1, 10))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[1] == ("overage", "Overage: Basic", 500)


def test_plan_change_splits_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_change_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15 of 30 days each
    invoice = billing.generate_invoice("s1")
    # 3000 * 15/30 = 1500 ; 1005 * 15/30 = 502.5 -> 503
    assert [i.amount_cents for i in invoice.line_items] == [1500, 503]


def test_usage_attributed_per_segment_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic: floor(100*10/30)=33 included ; pro: floor(300*20/30)=200 included
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))   # basic segment
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))  # pro segment (boundary)
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[2:] == [
        ("overage", "Overage: Basic", 7 * 10),
        ("overage", "Overage: Pro", 50 * 5),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")  # period is now [Jan 31, Mar 2) = 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # 10 days basic, 20 pro
    billing.record_usage("s1", "late", 43, date(2026, 1, 1))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[2:] == [("overage", "Overage: Basic", 10 * 10)]


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


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    bad = [
        ("basic", date(2026, 1, 1)),   # == period_start
        ("basic", date(2026, 1, 31)),  # == period_end
        ("basic", date(2026, 2, 5)),   # after period_end
        ("basic", date(2026, 1, 11)),  # not after previous change
        ("basic", date(2026, 1, 5)),   # before previous change
        ("pro", date(2026, 1, 20)),    # already current plan
    ]
    for plan_id, day in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    assert [c.plan_id for c in store.get_subscription("s1").plan_changes] == ["pro"]


def test_change_to_current_plan_without_changes_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_change_back_to_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    billing.change_plan("s1", "basic", date(2026, 1, 20))


def test_changes_are_cleared_after_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))  # overage 500
    invoice = billing.generate_invoice("s1", discount_code="TEN")
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 3000 + 500 - 350 - 500 + 265
    assert customer.credit_balance_cents == 0
