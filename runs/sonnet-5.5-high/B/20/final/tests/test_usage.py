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
    store.add_plan(Plan("max", "Max", 9000))


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_validates_input(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_is_not_billed_yet(billing):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert kinds(second)[1] == ("overage", "Overage: Basic", 200 * 1)


def test_event_is_billed_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in second.line_items] == ["plan"]
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is False


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[-1] == ("overage", "Overage: Basic", 100)


def test_change_plan_splits_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_subscription("s1").plan_changes == []


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 45))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "max", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[0] == ("plan", "Plan: Odd", 2)
    assert kinds(invoice)[1] == ("plan", "Plan: Max", 8700)


def test_usage_is_attributed_to_segments(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))
    billing.record_usage("s1", "c", 20, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 150),
    ]


def test_late_events_go_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 20, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[-1] == ("overage", "Overage: Basic", 100)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
    ]
    assert store.get_subscription("s1").plan_id == "max"


def test_changes_can_return_to_an_earlier_plan(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))


@pytest.mark.parametrize(
    "plan_id,effective_on",
    [
        ("pro", date(2026, 1, 1)),
        ("pro", date(2025, 12, 31)),
        ("pro", date(2026, 1, 31)),
        ("pro", date(2026, 2, 5)),
        ("basic", date(2026, 1, 10)),
    ],
)
def test_invalid_change_is_rejected(billing, store, plan_id, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    for plan_id, day in [("max", 11), ("max", 5), ("pro", 20)]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, date(2026, 1, day))

    assert store.get_subscription("s1").plan_changes == [(date(2026, 1, 11), "pro")]


def test_unknown_plan_and_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))

    assert store.get_subscription("s1").plan_changes == []


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 100),
        ("discount", -310),
        ("credit", -500),
        ("tax", 229),
    ]
    assert invoice.total_cents == sum(i.amount_cents for i in invoice.line_items)
    assert customer.credit_balance_cents == 0


def test_included_units_are_prorated_with_floor(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 11, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[2] == ("overage", "Overage: Basic", 10)
