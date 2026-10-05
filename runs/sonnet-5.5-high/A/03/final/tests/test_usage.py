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
    store.add_plan(Plan("team", "Team", 9000))


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent_and_ignores_changes(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 500)


def test_event_id_is_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_event_id_stays_deduplicated_after_billing(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is False
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_is_not_billed_until_its_period(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Basic", 500)


def test_late_event_is_billed_on_open_period_once(billing):
    billing.generate_invoice("s1")  # period is now [Jan 31, Mar 2)
    billing.record_usage("s1", "late", 110, date(2026, 1, 10))

    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(second)[1] == ("overage", "Overage: Basic", 100)
    assert [i.kind for i in third.line_items] == ["plan"]


def test_plan_change_splits_period(billing, store):
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


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1))
    store.get_plan("basic").monthly_price_cents = 1
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15/30 of 1 cent each

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [1, 1]


def test_overage_is_per_segment_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic segment: included 100*10//30 = 33; pro: 300*20//30 = 200
    billing.record_usage("s1", "a", 50, date(2026, 1, 10))
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [
        ("overage", "Overage: Basic", 17 * 10),
        ("overage", "Overage: Pro", 50 * 5),
    ]
    assert invoice.total_cents == 1000 + 4000 + 170 + 250


def test_late_events_attributed_to_first_segment(billing):
    billing.generate_invoice("s1")  # period [Jan 31, Mar 2): 30 days
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 133, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    # first segment is 10 days of basic: included 33, overage 100 at 10c
    assert lines(invoice)[2] == ("overage", "Overage: Basic", 1000)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.change_plan("s1", "basic", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Team",
        "Plan: Basic",
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_invalid_changes_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    bad = [
        ("pro", date(2026, 1, 15)),  # already current
        ("team", date(2026, 1, 11)),  # not after previous change
        ("team", date(2026, 1, 5)),  # before previous change
        ("team", date(2026, 1, 31)),  # == period_end
        ("team", date(2026, 2, 5)),  # after period_end
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


def test_change_on_period_start_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 1))


def test_change_back_to_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    assert len(billing.generate_invoice("s1").line_items) == 3


def test_changes_cleared_after_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")
    # the new period [Jan 31, Mar 2) is open for changes again
    billing.change_plan("s1", "basic", date(2026, 2, 5))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == ["Plan: Pro", "Plan: Basic"]


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


def test_unknown_discount_code_changes_nothing(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 5, date(2026, 1, 5))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert len(subscription.plan_changes) == 1
    assert not subscription.usage_events["e1"].billed
