from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# Fixture period is [2026-01-01, 2026-01-31): 30 days.


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=5)
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
    # Rejected calls record nothing, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_is_idempotent_per_subscription(billing, store):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 50 * 10)


def test_duplicate_event_ignored_after_it_was_billed(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False
    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_same_event_id_on_another_subscription_is_new(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Basic", 3000)]


def test_future_event_is_billed_on_the_period_it_belongs_to(billing):
    billing.record_usage("s1", "future", 150, date(2026, 1, 31))  # == period_end
    billing.record_usage("s1", "inside", 101, date(2026, 1, 30))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[1] == ("overage", "Overage: Basic", 10)
    assert lines(second)[1] == ("overage", "Overage: Basic", 50 * 10)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in second.line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing, store):
    billing.generate_invoice("s1")  # period is now [01-31, 03-02)
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 300)


def test_change_plan_prorates_plan_lines(billing, store):
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


def test_prorated_amount_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1))
    # 15 of 30 days of a 1 cent plan is exactly 0.5 cents -> 1.
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Odd", 1),
    ]


def test_new_plan_stays_after_the_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 7))  # basic 6 days
    billing.change_plan("s1", "basic", date(2026, 1, 21))  # pro 14, basic 10

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 600),
        ("plan", "Plan: Pro", 2800),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_invalid_changes_are_rejected_and_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    bad = [
        ("basic", date(2026, 1, 1)),  # == period_start
        ("basic", date(2026, 1, 31)),  # == period_end
        ("basic", date(2026, 2, 5)),  # after period_end
        ("basic", date(2025, 12, 20)),  # before period_start
        ("basic", date(2026, 1, 11)),  # == previous change
        ("basic", date(2026, 1, 5)),  # before previous change
        ("pro", date(2026, 1, 20)),  # already current
    ]
    for plan_id, effective_on in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert subscription.plan_changes == before
    assert subscription.plan_id == "basic"


def test_change_to_the_starting_plan_is_rejected_without_changes(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_change_back_to_original_plan_is_allowed_after_a_switch(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))


def test_usage_is_attributed_to_segments_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 basic / 20 pro
    # basic allowance: 100*10//30 = 33; pro allowance: 300*20//30 = 200
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # basic
    billing.record_usage("s1", "b", 150, date(2026, 1, 11))  # pro (boundary)
    billing.record_usage("s1", "c", 100, date(2026, 1, 30))  # pro

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 7 * 10),
        ("overage", "Overage: Pro", 50 * 5),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")  # open period [01-31, 03-02): 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # Basic for 10 of 30 days
    billing.record_usage("s1", "late", 50, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    # basic allowance = 100*10//30 = 33 -> 17 overage; pro has none.
    assert lines(invoice)[2:] == [("overage", "Overage: Basic", 170)]


def test_floor_division_of_included_units(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 2))  # basic 1 day: 100//30 = 3
    billing.record_usage("s1", "a", 4, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [("overage", "Overage: Basic", 10)]


def test_discount_credit_and_tax_apply_to_overage_too(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 200, date(2026, 1, 5))  # 100 over -> 1000

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 1000),
        ("discount", "Discount", -400),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice
