from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

START = date(2026, 1, 1)


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=5)
    )
    store.add_plan(Plan("max", "Max", 9000))


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validates_and_is_idempotent(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, START)
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, START)
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, START)

    assert billing.record_usage("s1", "e1", 10, START) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 20)) is False
    # The first call wins.
    assert store.get_unbilled_usage("s1")[0].units == 10


def test_event_ids_are_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(Subscription("s2", "c1", "basic", START, date(2026, 1, 31)))
    assert billing.record_usage("s1", "e1", 1, START) is True
    assert billing.record_usage("s2", "e1", 1, START) is True


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, START)
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Basic", 3000)]


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 90, START)
    billing.record_usage("s1", "e2", 30, date(2026, 1, 30))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 200),
    ]
    assert invoice.total_cents == 3200


def test_event_on_period_end_is_billed_next_period_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]

    second = billing.generate_invoice("s1")
    assert lines(second)[1] == ("overage", "Overage: Basic", 500)

    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # period now [Jan 31, Mar 2)
    billing.record_usage("s1", "late", 110, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 100) in lines(invoice)


def test_failed_invoice_does_not_consume_usage(billing, store):
    billing.record_usage("s1", "e1", 150, START)
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")
    assert len(store.get_unbilled_usage("s1")) == 1
    assert store.get_subscription("s1").period_start == START


def test_change_plan_prorates_segments(billing, store):
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


def test_proration_rounds_half_up(billing):
    # 3000 * 1 / 30 = 100 ; use a plan whose share is an exact half.
    billing._store.add_plan(Plan("odd", "Odd", 1005))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15/30 of 1005 = 502.5
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[:2] == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Odd", 503),
    ]


def test_usage_attributed_per_segment_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic segment: 10 days -> included 100*10//30 = 33
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    # pro segment: 20 days -> included 300*20//30 = 200
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))
    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 70),
        ("overage", "Overage: Pro", 250),
    ]


def test_late_usage_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 60, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1")
    # Period Jan 31 - Mar 2 (30 days); basic segment Jan 31 - Feb 10 = 10 days.
    assert ("overage", "Overage: Basic", (60 - 33) * 10) in lines(invoice)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.change_plan("s1", "basic", date(2026, 1, 26))
    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 1500),
        ("plan", "Plan: Basic", 500),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_can_return_to_original_plan_after_change(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    invoice = billing.generate_invoice("s1")
    assert [i.amount_cents for i in invoice.line_items] == [1000, 2000, 1000]


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),  # == period_start
        ("pro", date(2025, 12, 31)),
        ("pro", date(2026, 1, 31)),  # == period_end
        ("pro", date(2026, 2, 5)),
        ("basic", date(2026, 1, 10)),  # already current
    ],
)
def test_invalid_change_raises_and_leaves_state(billing, store, plan_id, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1").plan_changes == []
    assert store.get_subscription("s1").plan_id == "basic"


def test_change_must_be_after_previous_change_and_not_current(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for plan_id, day in [
        ("max", date(2026, 1, 11)),  # same day
        ("max", date(2026, 1, 5)),  # before previous
        ("pro", date(2026, 1, 20)),  # already current
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    assert store.get_subscription("s1").plan_changes == [(date(2026, 1, 11), "pro")]


def test_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 150, START)  # 50 over -> 500

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 3500, discount 350, credit 500, taxable 2650, tax 265
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_next_period_starts_clean_on_new_plan(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")
    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]
