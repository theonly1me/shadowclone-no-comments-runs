from datetime import date

import pytest

from ledger.models import Plan


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
            included_units=60,
            overage_unit_price_cents=5,
        )
    )


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_validates_input(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent_per_subscription(billing):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 1000, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_duplicate_event_id_after_billing_is_still_ignored(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 20)) is False
    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_future_event_is_not_billed_yet_and_billed_once(billing):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Basic", 200)
    assert [item.kind for item in third.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 31, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 10)


def test_usage_aggregates_across_events(billing):
    billing.record_usage("s1", "e1", 20, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 20, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_change_plan_splits_period(billing, store):
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


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Odd", 1)
    assert lines(invoice)[1] == ("plan", "Plan: Basic", 1500)


def test_multiple_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]


def test_usage_is_attributed_to_segments_with_prorated_allowance(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 15, date(2026, 1, 10))
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 50),
        ("overage", "Overage: Pro", 50),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == [
        "plan",
        "plan",
        "overage",
    ]
    assert lines(invoice)[2][1] == "Overage: Basic"


def test_overage_is_subject_to_discount_credit_and_tax(billing, store):
    from decimal import Decimal

    from ledger.models import DiscountCode

    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 80, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 500),
        ("discount", "Discount", -350),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),
        ("pro", date(2025, 12, 31)),
        ("pro", date(2026, 1, 31)),
        ("pro", date(2026, 2, 5)),
        ("basic", date(2026, 1, 10)),
    ],
)
def test_invalid_change_raises_and_leaves_state(billing, store, plan_id, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    for effective_on in (date(2026, 1, 11), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "basic", effective_on)
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_plan_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))

    assert store.get_subscription("s1").plan_changes == []
