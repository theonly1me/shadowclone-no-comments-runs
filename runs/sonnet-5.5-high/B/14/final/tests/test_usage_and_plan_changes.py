from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan(
            plan_id="basic",
            name="Basic",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_is_idempotent_per_event_id(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False


def test_same_event_id_on_another_subscription_is_recorded(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 5, date(2026, 1, 5)) is True


def test_duplicate_event_does_not_change_billing(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_duplicate_of_billed_event_is_still_ignored(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 2, 5)) is False
    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_record_usage_validates_input(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_overage_line_is_billed(billing):
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 15, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
    ]
    assert invoice.total_cents == 3050


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_event_waits_for_its_period(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Basic", 100)


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2025, 12, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_late_event_is_attributed_to_first_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "late", 20, date(2025, 12, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
    ]


def test_change_plan_splits_the_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_following_period_bills_the_new_plan_in_full(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_segment_prices_are_rounded_half_up(billing):
    billing.change_plan("s1", "odd", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 100),
        ("plan", "Plan: Odd", 967),
    ]


def test_half_cent_rounds_up(billing, store):
    store.add_plan(Plan(plan_id="half", name="Half", monthly_price_cents=15))
    billing.change_plan("s1", "half", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("plan", "Plan: Half", 8)


def test_usage_is_attributed_by_date_and_allowance_is_prorated(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 5, date(2026, 1, 10))
    billing.record_usage("s1", "b", 70, date(2026, 1, 11))
    billing.record_usage("s1", "c", 10, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Pro", 70),
    ]


def test_basic_segment_overage_uses_floor_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2] == ("overage", "Overage: Basic", 100)


def test_overage_lines_follow_all_plan_lines_in_order(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 100, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 170),
    ]


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_discount_credit_and_tax_apply_to_plans_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 80, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Pro", 70),
        ("discount", "Discount", -507),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 406),
    ]
    assert invoice.total_cents == 4469
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_must_fall_strictly_inside_the_period(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 21))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_unknown_plan_and_subscription_raise_key_error(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_unknown_discount_code_leaves_state_unchanged(billing, store):
    billing.record_usage("s1", "a", 40, date(2026, 1, 5))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert len(subscription.plan_changes) == 1
    assert len(store.get_unbilled_usage("s1")) == 1
