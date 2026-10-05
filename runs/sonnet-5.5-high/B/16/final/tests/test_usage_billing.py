from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_duplicate_event_is_billed_once_with_first_values(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Metered", 100)


def test_overage_billed_once_and_future_events_wait(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 35, date(2026, 1, 30))
    billing.record_usage("s1", "e2", 7, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    assert lines(first) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 50),
    ]

    second = billing.generate_invoice("s1")
    assert [i.kind for i in second.line_items] == ["plan"]


def test_late_event_is_billed_in_open_period(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 31, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Metered", 10)


def test_event_not_yet_due_is_not_lost(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "future", 100, date(2026, 2, 15))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[1] == ("overage", "Overage: Metered", 700)


def test_plan_change_splits_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_plan_change_persists_and_clears(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert billing.generate_invoice("s1").total_cents == 6000


def test_multiple_changes_and_rounding(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 12))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 200),
        ("plan", "Plan: Basic", 1900),
    ]


def test_usage_attributed_per_segment(billing, store):
    billing.change_plan("s1", "metered", date(2026, 1, 16))
    billing.record_usage("s1", "a", 20, date(2026, 1, 3))
    billing.record_usage("s1", "b", 20, date(2026, 1, 20))
    billing.record_usage("s1", "c", 5, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Metered", 1500),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Metered", 100),
    ]


def test_late_event_attributed_to_first_segment(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.change_plan("s1", "basic", date(2026, 2, 15))
    billing.record_usage("s1", "late", 100, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [(i.kind, i.description) for i in invoice.line_items] == [
        ("plan", "Plan: Metered"),
        ("plan", "Plan: Basic"),
        ("overage", "Overage: Metered"),
    ]


def test_included_units_prorated_with_floor(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 11, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2] == ("overage", "Overage: Metered", 10)


def test_full_ordering_with_discount_credit_tax(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "metered", date(2026, 1, 16))
    billing.record_usage("s1", "a", 25, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 1500),
        ("plan", 1500),
        ("overage", 100),
        ("discount", -310),
        ("credit", -500),
        ("tax", 229),
    ]
    assert invoice.total_cents == sum(i.amount_cents for i in invoice.line_items)
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),
        ("pro", date(2026, 1, 31)),
        ("pro", date(2026, 3, 1)),
        ("basic", date(2026, 1, 10)),
    ],
)
def test_invalid_change_rejected(billing, store, plan_id, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    for bad_plan, bad_date in [("basic", date(2026, 1, 10)), ("basic", date(2026, 1, 5)), ("pro", date(2026, 1, 20))]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", bad_plan, bad_date)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_unknown_plan_rejected(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_change_back_to_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    billing.change_plan("s1", "basic", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]
