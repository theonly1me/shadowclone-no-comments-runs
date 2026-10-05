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


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent_per_subscription(billing, store):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]


def test_same_event_id_on_other_subscription_is_recorded(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_is_billed_in_its_own_period_once(billing):
    billing.record_usage("s1", "future", 31, date(2026, 1, 31))  # == period_end

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Basic", 10)
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # open period is now 1/31 - 3/2
    billing.record_usage("s1", "late", 31, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 10)


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    subscription = store.get_subscription("s1")
    bad_dates = [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31)]
    for bad in bad_dates:
        with pytest.raises(ValueError):
            billing.change_plan("s1", "pro", bad)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert subscription.plan_changes == []

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for bad_date, plan in [(date(2026, 1, 11), "basic"), (date(2026, 1, 5), "basic")]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan, bad_date)
    with pytest.raises(ValueError):  # already on pro
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    assert len(subscription.plan_changes) == 1


def test_single_change_prorates_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_subscription("s1").plan_changes == []


def test_plan_lines_round_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))  # 15/30 of 1005 = 502.5

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Odd", 503)


def test_usage_attributed_to_segments_with_floor_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic segment: 10 days -> included 30*10//30 = 10; pro: 20 days -> 40
    billing.record_usage("s1", "a", 15, date(2026, 1, 10))  # basic
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))  # pro (boundary)
    billing.record_usage("s1", "c", 1, date(2025, 12, 20))  # late -> first segment

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 60),  # (16 - 10) * 10
        ("overage", "Overage: Pro", 50),  # (50 - 40) * 5
    ]
    assert invoice.total_cents == 5110


def test_allowance_uses_integer_floor(billing, store):
    store.add_plan(Plan("tiny", "Tiny", 3000, included_units=10, overage_unit_price_cents=1))
    store.get_subscription("s1").plan_id = "tiny"
    billing.change_plan("s1", "basic", date(2026, 1, 21))  # tiny 20 days: 10*20//30 = 6
    billing.record_usage("s1", "a", 7, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Tiny", 1) in lines(invoice)


def test_multiple_changes_and_plan_in_effect_at_period_end(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"
    # Next period starts fresh on the final plan.
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 50, date(2026, 1, 5))  # 20 over -> 200

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


def test_failed_invoice_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2026, 1, 12))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert len(subscription.plan_changes) == 1
    invoice = billing.generate_invoice("s1")
    assert any(i.kind == "overage" for i in invoice.line_items)
