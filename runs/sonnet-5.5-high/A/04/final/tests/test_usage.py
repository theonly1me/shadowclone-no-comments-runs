from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=5)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=2)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False
    assert billing.record_usage("s1", "e2", 1, date(2026, 1, 6))


def test_same_event_id_on_other_subscription_is_distinct(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5))


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    # the rejected calls did not consume the event id
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 70, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 50, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]
    assert invoice.total_cents == 3100


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_event_is_billed_once_and_future_event_waits(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 30))
    billing.record_usage("s1", "future", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[1] == ("overage", "Overage: Basic", 250)
    # period 2 is 1/31 - 3/2 (30 days); only the future event remains
    assert lines(second)[1] == ("overage", "Overage: Basic", 250)
    assert len([i for i in second.line_items if i.kind == "overage"]) == 1
    third = billing.generate_invoice("s1")
    assert [i.kind for i in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 110, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 50) in lines(invoice)


def test_duplicate_of_billed_event_is_not_billed_again(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 20)) is False
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_plan_change_splits_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_plan_change_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 5))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15/30 of 3000, 15/30 of 5

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [1500, 3]


def test_usage_attributed_per_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 50, date(2026, 1, 10))  # basic, allowance 33
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))  # pro, allowance 200
    billing.record_usage("s1", "c", 300, date(2026, 1, 30))  # pro

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 17 * 5),
        ("overage", "Overage: Pro", 150 * 2),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")  # next period 1/31 - 3/2
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 200, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    # first segment (Basic) is 10 of 30 days: allowance 33
    assert ("overage", "Overage: Basic", 167 * 5) in lines(invoice)
    assert not any(i.description == "Overage: Pro" for i in invoice.line_items)


def test_multiple_changes_and_plan_carries_over(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []

    billing.change_plan("s1", "pro", date(2026, 2, 5))
    billing.generate_invoice("s1")
    assert store.get_subscription("s1").plan_id == "pro"
    assert [i.description for i in billing.generate_invoice("s1").line_items] == [
        "Plan: Pro"
    ]


def test_invalid_changes_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    for args, error in [
        (("basic", date(2026, 1, 1)), ValueError),  # == period_start
        (("basic", date(2026, 1, 31)), ValueError),  # == period_end
        (("basic", date(2026, 2, 5)), ValueError),  # after period
        (("basic", date(2026, 1, 11)), ValueError),  # == previous change
        (("basic", date(2026, 1, 5)), ValueError),  # before previous change
        (("pro", date(2026, 1, 20)), ValueError),  # already current
        (("ghost", date(2026, 1, 20)), KeyError),  # unknown plan
    ]:
        with pytest.raises(error):
            billing.change_plan("s1", *args)
        assert subscription.plan_changes == before
        assert subscription.plan_id == "basic"

    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))


def test_change_back_to_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 12))


def test_change_to_current_plan_without_prior_changes_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_full_stack_discount_credit_tax(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 53, date(2026, 1, 10))  # 20 over -> 100

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1000 + 4000 + 100 = 5100
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("discount", "Discount", -510),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 409),
    ]
    assert invoice.total_cents == 5100 - 510 - 500 + 409
    assert invoice.total_cents == sum(i.amount_cents for i in invoice.line_items)
    assert customer.credit_balance_cents == 0


def test_unknown_discount_code_changes_nothing(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 500, date(2026, 1, 10))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert len(subscription.plan_changes) == 1
    assert ("overage", "Overage: Basic", 467 * 5) in lines(billing.generate_invoice("s1"))
