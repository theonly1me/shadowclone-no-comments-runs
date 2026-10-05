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


def test_record_usage_validates(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 500)


def test_idempotency_survives_billing(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is False
    invoice = billing.generate_invoice("s1")
    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_usage_within_included_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))
    invoice = billing.generate_invoice("s1")
    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_future_event_waits_for_its_period(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))
    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]

    second = billing.generate_invoice("s1")
    assert kinds(second)[1] == ("overage", "Overage: Basic", 500)


def test_late_event_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 130, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 300)


def test_plan_change_splits_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_plan_change_usage_attribution_and_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # Basic: floor(100*10/30)=33 included. Pro: floor(300*20/30)=200 included.
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    billing.record_usage("s1", "b", 210, date(2026, 1, 11))
    billing.record_usage("s1", "c", 1, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 70),
        ("overage", "Overage: Pro", 55),
    ]


def test_late_event_attributed_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 100, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    # Period 01-31..03-02 (30 days): basic 10 days -> included 33, overage 67.
    assert ("overage", "Overage: Basic", 670) in kinds(invoice)
    assert not any(d == "Overage: Pro" for _, d, _ in kinds(invoice))


def test_multiple_changes_and_plan_rolls_over(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []

    billing.change_plan("s1", "pro", date(2026, 2, 5))
    billing.generate_invoice("s1")
    assert store.get_subscription("s1").plan_id == "pro"
    assert [i.amount_cents for i in billing.generate_invoice("s1").line_items] == [
        6000
    ]


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1001))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15/30 of 1001 = 500.5

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [1500, 501]


def test_change_plan_rejections_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")

    bad = [
        ("pro", date(2026, 1, 20), ValueError),  # same as current
        ("basic", date(2026, 1, 11), ValueError),  # not after previous change
        ("basic", date(2026, 1, 5), ValueError),  # before previous change
        ("basic", date(2026, 1, 1), ValueError),  # == period_start
        ("basic", date(2026, 1, 31), ValueError),  # == period_end
        ("basic", date(2026, 2, 15), ValueError),  # after period_end
        ("ghost", date(2026, 1, 20), KeyError),
    ]
    for plan_id, day, error in bad:
        with pytest.raises(error):
            billing.change_plan("s1", plan_id, day)
        assert subscription.plan_changes == [(date(2026, 1, 11), "pro")]

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 1))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_change_back_to_original_plan_is_same_as_current_check(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_discount_credit_tax_after_overage(billing, store):
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


def test_failed_invoice_does_not_consume_usage(billing, store):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")

    invoice = billing.generate_invoice("s1")
    assert kinds(invoice)[1] == ("overage", "Overage: Basic", 500)
