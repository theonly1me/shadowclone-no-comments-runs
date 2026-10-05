from datetime import date
from decimal import Decimal

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
    store.add_plan(Plan(plan_id="max", name="Max", monthly_price_cents=9000))


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_validates(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -2, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_record_usage_is_idempotent_per_subscription(billing):
    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is False
    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_future_event_waits_for_its_period(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Basic", 100)


def test_late_event_is_billed_in_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 50, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 200)


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan"]


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    bad_calls = [
        ("pro", date(2026, 1, 20), ValueError),
        ("basic", date(2026, 1, 11), ValueError),
        ("basic", date(2026, 1, 5), ValueError),
        ("basic", date(2026, 1, 1), ValueError),
        ("basic", date(2026, 1, 31), ValueError),
        ("basic", date(2026, 2, 5), ValueError),
        ("nope", date(2026, 1, 20), KeyError),
    ]
    for plan_id, day, error in bad_calls:
        with pytest.raises(error):
            billing.change_plan("s1", plan_id, day)

    subscription = store.get_subscription("s1")
    assert [(c.plan_id, c.effective_on) for c in subscription.plan_changes] == [
        ("pro", date(2026, 1, 11))
    ]
    assert subscription.plan_id == "basic"


def test_change_back_to_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))


def test_change_plan_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_single_change_prorates_segments(billing, store):
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


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=3005))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Odd", 1503)
    assert lines(invoice)[1] == ("plan", "Plan: Basic", 1500)


def test_multiple_changes_and_segment_usage(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))
    billing.record_usage("s1", "a", 20, date(2026, 1, 3))
    billing.record_usage("s1", "b", 50, date(2026, 1, 11))
    billing.record_usage("s1", "c", 5, date(2026, 1, 15))
    billing.record_usage("s1", "d", 100, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
        ("overage", "Overage: Basic", 100),
        ("overage", "Overage: Pro", 175),
        ("overage", "Overage: Max", 0),
    ]
    assert invoice.total_cents == 6275
    assert store.get_subscription("s1").plan_id == "max"


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 20, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2] == ("overage", "Overage: Basic", 100)
    assert len(invoice.line_items) == 3


def test_included_units_floor_per_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 10, date(2026, 1, 2))
    billing.record_usage("s1", "b", 20, date(2026, 1, 12))

    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan", "plan"]

    billing.change_plan("s1", "basic", date(2026, 2, 10))
    billing.record_usage("s1", "c", 21, date(2026, 2, 1))
    billing.record_usage("s1", "d", 25, date(2026, 2, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [
        ("overage", "Overage: Pro", 5),
        ("overage", "Overage: Basic", 50),
    ]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    from ledger.models import DiscountCode

    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 50, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -320),
        ("credit", -500),
        ("tax", 238),
    ]
    assert invoice.total_cents == 2618
    assert customer.credit_balance_cents == 0


def test_failed_invoice_does_not_consume_events(billing, store):
    billing.record_usage("s1", "a", 50, date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="missing")

    invoice = billing.generate_invoice("s1")
    assert any(i.kind == "overage" for i in invoice.line_items)
    assert len(store.get_subscription("s1").plan_changes) == 0
