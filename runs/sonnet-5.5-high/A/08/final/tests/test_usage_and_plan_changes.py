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
        Plan("pro", "Pro", 9000, included_units=300, overage_unit_price_cents=2)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


# --- usage ---


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50 * 5),
    ]


def test_same_event_id_on_other_subscription_is_independent(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_duplicate_of_billed_event_is_still_ignored(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False
    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    # The rejected call did not consume the event_id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_event_at_period_end_is_billed_on_next_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert lines(second) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50 * 5),
    ]


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    assert len(billing.generate_invoice("s1").line_items) == 2
    assert len(billing.generate_invoice("s1").line_items) == 1


def test_late_event_is_billed_on_open_period(billing):
    billing.record_usage("s1", "e1", 150, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 250) in lines(invoice)


def test_events_sum_across_a_period(billing):
    billing.record_usage("s1", "e1", 60, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 60, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Basic", 20 * 5)


# --- plan changes ---


def test_mid_cycle_change_splits_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    # basic: 10/30 days, pro: 20/30 days
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
    ]
    assert invoice.total_cents == 7000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_segment_prices_round_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 5))
    # 5 * 15 / 30 = 2.5 -> 3 for each segment
    billing.change_plan("s1", "odd", date(2026, 1, 16))
    store.get_plan("basic").monthly_price_cents = 5

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [3, 3]


def test_usage_attributed_to_segment_by_date(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic gets 100 * 10 // 30 = 33 included; pro gets 300 * 20 // 30 = 200
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # basic segment
    billing.record_usage("s1", "b", 100, date(2026, 1, 11))  # pro segment

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 7 * 5),
    ]


def test_overage_lines_follow_all_plan_lines_in_order(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2026, 1, 1))
    billing.record_usage("s1", "b", 300, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 67 * 5),
        ("overage", "Overage: Pro", 100 * 2),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Basic", 67 * 5)
    assert len(invoice.line_items) == 3


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 3000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_plan_changes_do_not_leak_into_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 9000)]


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),  # == period_start
        ("pro", date(2025, 12, 31)),  # before period
        ("pro", date(2026, 1, 31)),  # == period_end
        ("pro", date(2026, 2, 5)),  # after period
        ("basic", date(2026, 1, 10)),  # already current
    ],
)
def test_invalid_change_is_rejected_without_side_effects(
    billing, store, plan_id, effective_on
):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1").plan_changes == []
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_change_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = list(store.get_subscription("s1").plan_changes)

    for day in (date(2026, 1, 11), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "basic", day)
    with pytest.raises(ValueError):  # pro is already current
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert store.get_subscription("s1").plan_changes == before


def test_change_back_to_original_plan_is_allowed_after_a_change(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 12))  # no error


def test_change_plan_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


# --- totals ---


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 133, date(2026, 1, 3))  # 100 over -> 33 included

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1000 + 6000 + 100*5 = 7500
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Basic", 500),
        ("discount", "Discount", -750),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 625),
    ]
    assert invoice.total_cents == 6875
    assert customer.credit_balance_cents == 0


def test_failed_invoice_leaves_usage_unbilled(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="missing")

    invoice = billing.generate_invoice("s1")

    assert len(invoice.line_items) == 2
