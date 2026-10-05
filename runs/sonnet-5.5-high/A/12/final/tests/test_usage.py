from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=5)
    )
    store.add_plan(
        Plan("pro", "Pro", 9000, included_units=300, overage_unit_price_cents=2)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    # Rejected calls record nothing.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_is_idempotent_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 2, 20)) is False
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True

    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[1] == ("overage", "Overage: Basic", 50 * 5)


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1")
    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_event_billed_once(billing):
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))
    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan", "overage"]
    assert [i.kind for i in second.line_items] == ["plan"]


def test_future_event_waits_for_its_period(billing):
    # Period is [Jan 1, Jan 31); Jan 31 belongs to the next period.
    billing.record_usage("s1", "e1", 200, date(2026, 1, 31))
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]
    second = billing.generate_invoice("s1")
    assert lines(second)[1] == ("overage", "Overage: Basic", 500)


def test_late_event_billed_in_open_period_first_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "late", 200, date(2025, 12, 20))
    invoice = billing.generate_invoice("s1")
    # basic: 10/30 days -> included 33, overage 167 * 5
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 167 * 5),
    ]


def test_plan_change_splits_period_and_updates_subscription(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
    ]
    assert invoice.total_cents == 7000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []

    nxt = billing.generate_invoice("s1")
    assert lines(nxt) == [("plan", "Plan: Pro", 9000)]


def test_plan_line_rounds_half_up(store, billing):
    store.add_plan(Plan("odd", "Odd", 5))
    store.add_plan(Plan("odd2", "Odd2", 5))
    # 5 * 15 / 30 = 2.5 -> 3 for each segment
    billing.change_plan("s1", "odd", date(2026, 1, 16))
    store.get_subscription("s1").plan_id = "odd2"
    invoice = billing.generate_invoice("s1")
    assert [i.amount_cents for i in invoice.line_items] == [3, 3]


def test_usage_attributed_to_segment_with_floored_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # basic, included 33
    billing.record_usage("s1", "b", 500, date(2026, 1, 11))  # pro, included 200
    billing.record_usage("s1", "c", 10, date(2026, 1, 30))  # pro
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 7 * 5),
        ("overage", "Overage: Pro", 310 * 2),
    ]


def test_multiple_changes_including_back_to_original(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 6))
    billing.change_plan("s1", "basic", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 500),
        ("plan", "Plan: Pro", 3000),
        ("plan", "Plan: Basic", 1500),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_invalid_changes_leave_state_unchanged(billing, store):
    subscription = store.get_subscription("s1")
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    bad = [
        ("pro", date(2026, 1, 20)),  # already current
        ("basic", date(2026, 1, 1)),  # not after period_start
        ("basic", date(2026, 1, 31)),  # not before period_end
        ("basic", date(2026, 1, 11)),  # not after previous change
        ("basic", date(2026, 1, 5)),  # before previous change
    ]
    for plan_id, on in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, on)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))

    assert subscription.plan_changes == [(date(2026, 1, 11), "pro")]
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_without_changes_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 43, date(2026, 1, 2))  # 10 overage * 5 = 50
    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1000 + 6000 + 50 = 7050; discount 705; credit 500; tax 585
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 1000),
        ("plan", 6000),
        ("overage", 50),
        ("discount", -705),
        ("credit", -500),
        ("tax", 585),
    ]
    assert invoice.total_cents == 7050 - 705 - 500 + 585
    assert customer.credit_balance_cents == 0
