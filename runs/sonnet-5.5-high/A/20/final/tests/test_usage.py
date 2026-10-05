from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    # Period is Jan 1 - Jan 31: 30 days.
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=5)
    )
    store.add_plan(
        Plan("pro", "Pro", 9000, included_units=300, overage_unit_price_cents=2)
    )


def kinds(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False
    invoice = billing.generate_invoice("s1")
    # 10 units only, within the 100 included.
    assert kinds(invoice) == [("plan", "Plan: Basic", 3000)]


def test_same_event_id_on_different_subscriptions(billing, store):
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
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    # Rejected calls did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 70, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 50, date(2026, 1, 30))
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]
    assert invoice.total_cents == 3100


def test_future_event_billed_on_later_invoice_only_once(billing):
    billing.record_usage("s1", "future", 150, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]
    second = billing.generate_invoice("s1")
    assert [i.kind for i in second.line_items] == ["plan", "overage"]
    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # now Jan 31 - Mar 2 (30 days)
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 150)


def test_change_plan_splits_invoice(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_plan_lines_round_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15 of 30 days
    invoice = billing.generate_invoice("s1")
    # 3000 * 15/30 = 1500, 1005 * 15/30 = 502.5 -> 503
    assert [i.amount_cents for i in invoice.line_items] == [1500, 503]


def test_usage_attributed_to_segments(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Basic: 10 days -> included 100*10//30 = 33. Pro: 20 days -> 300*20//30 = 200.
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # basic, 7 over
    billing.record_usage("s1", "b", 210, date(2026, 1, 11))  # pro (boundary), 10 over
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 35),
        ("overage", "Overage: Pro", 20),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # 10 days basic, 20 pro
    billing.record_usage("s1", "late", 43, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[2] == ("overage", "Overage: Basic", 50)  # 43-33=10 * 5
    assert len(invoice.line_items) == 3


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert [(i.description, i.amount_cents) for i in invoice.line_items] == [
        ("Plan: Basic", 1000),
        ("Plan: Pro", 3000),
        ("Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_change_plan_rejections_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    for plan, day in [
        ("basic", date(2026, 1, 1)),  # == period_start
        ("basic", date(2026, 1, 31)),  # == period_end
        ("basic", date(2026, 1, 11)),  # == previous change
        ("basic", date(2026, 1, 5)),  # before previous change
        ("pro", date(2026, 1, 20)),  # already current
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan, day)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert subscription.plan_changes == before
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_without_changes_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_discount_credit_tax_on_usage_invoice(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))  # 200 over -> 1000
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


def test_unknown_discount_code_leaves_state_unchanged(billing, store):
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")
    invoice = billing.generate_invoice("s1")
    assert invoice.line_items[1].kind == "overage"
