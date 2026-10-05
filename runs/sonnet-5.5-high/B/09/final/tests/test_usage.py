from datetime import date

import pytest

from ledger.models import Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan(
            "basic",
            "Basic",
            3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=5)
    )
    store.add_plan(Plan("max", "Max", 9000))


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False


def test_same_event_id_on_other_subscription_is_recorded(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 5, date(2026, 1, 5)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 10, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 50),
    ]
    assert invoice.total_cents == 3050


def test_usage_within_included_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_future_event_is_not_billed_yet_and_is_billed_once(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 31))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1] == ("overage", "Overage: Basic", 100)
    assert lines(second) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 700),
    ]
    assert lines(third) == [("plan", "Plan: Basic", 3000)]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_late_event_is_attributed_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 15))
    billing.record_usage("s1", "late", 40, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Overage: Basic",
    ]


def test_change_plan_prorates_segments(billing, store):
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
    assert subscription.period_start == date(2026, 1, 31)


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 15))
    billing.change_plan("s1", "odd", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [100, 15]


def test_segment_included_units_are_floored_and_usage_split(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 11, date(2026, 1, 10))
    billing.record_usage("s1", "b", 41, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 5),
    ]


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.change_plan("s1", "basic", date(2026, 1, 26))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Max",
        "Plan: Basic",
    ]
    assert [i.amount_cents for i in invoice.line_items] == [1000, 2000, 1500, 500]
    assert store.get_subscription("s1").plan_id == "basic"


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    subscription = store.get_subscription("s1")
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    for plan_id, day in [
        ("max", date(2026, 1, 1)),
        ("max", date(2026, 1, 31)),
        ("max", date(2026, 1, 11)),
        ("max", date(2026, 1, 5)),
        ("pro", date(2026, 1, 20)),
        ("max", date(2026, 2, 20)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "max", date(2026, 1, 20))

    assert subscription.plan_changes == [(date(2026, 1, 11), "pro")]
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_without_changes_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_changes_do_not_carry_into_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_discount_credit_and_tax_apply_to_overage(billing, store):
    from decimal import Decimal

    from ledger.models import DiscountCode

    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
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
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0
