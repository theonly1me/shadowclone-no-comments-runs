from datetime import date

import pytest

from ledger.models import Plan


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


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
    ]


def test_same_event_id_on_other_subscription_is_separate(billing, store):
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
    # the rejected call did not consume the event id
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_event_is_billed_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    second = billing.generate_invoice("s1")
    assert [i.kind for i in second.line_items] == ["plan"]


def test_event_at_or_after_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "future", 150, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]

    second = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 500) in lines(second)


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")  # period is now [Jan 31, Mar 2)
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 300) in lines(invoice)


def test_billed_event_id_stays_idempotent_across_periods(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is False
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_mid_cycle_change_prorates_plans(billing, store):
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


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15/30 of 1005 = 502.5

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("plan", "Plan: Odd", 503)


def test_usage_is_attributed_to_segment_of_occurrence(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic segment: 10 days -> included 100*10//30 = 33
    billing.record_usage("s1", "a", 43, date(2026, 1, 10))  # overage 10 * 10
    # pro segment: 20 days -> included 300*20//30 = 200
    billing.record_usage("s1", "b", 210, date(2026, 1, 11))  # overage 10 * 5

    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 50),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")  # period now [Jan 31, Mar 2)
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # 10 days basic, 20 pro
    billing.record_usage("s1", "late", 43, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 100) in lines(invoice)
    assert all(d != "Overage: Pro" for _, d, _ in lines(invoice))


def test_several_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_invalid_changes_are_rejected_without_side_effects(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    bad = [
        ("basic", date(2026, 1, 1)),  # == period_start
        ("basic", date(2026, 1, 31)),  # == period_end
        ("basic", date(2026, 2, 5)),  # after the period
        ("basic", date(2026, 1, 11)),  # not after previous change
        ("basic", date(2026, 1, 5)),  # before previous change
        ("pro", date(2026, 1, 20)),  # already on pro
    ]
    for plan_id, day in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
        assert subscription.plan_changes == before

    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))
    assert subscription.plan_changes == before
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_without_prior_change_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_discount_credit_tax_apply_to_plans_and_overage(billing, store):
    from decimal import Decimal

    from ledger.models import DiscountCode

    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))  # overage 500

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 3500 -> discount 350 -> credit 500 -> tax 265
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_plan_changes_cleared_so_next_period_is_single_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    second = billing.generate_invoice("s1")
    assert lines(second) == [("plan", "Plan: Pro", 6000)]
