from datetime import date

import pytest

from ledger.models import Plan


@pytest.fixture(autouse=True)
def metered_plans(store):
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
            included_units=90,
            overage_unit_price_cents=5,
        )
    )


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_is_idempotent_per_event_id(billing, store):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 20)) is False

    events = store.get_usage_events("s1")
    assert [(e.units, e.occurred_on) for e in events] == [(5, date(2026, 1, 2))]


def test_same_event_id_on_another_subscription_is_recorded(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "e1", 5, date(2026, 1, 2)) is True


def test_record_usage_rejects_non_positive_units(billing, store):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", -3, date(2026, 1, 2))
    assert store.get_usage_events("s1") == []


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 2))


def test_usage_within_allowance_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_overage_is_billed_after_plan_line(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 15, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]
    assert invoice.total_cents == 3100


def test_event_at_period_end_is_not_billed_yet(billing, store):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[1] == ("overage", "Overage: Basic", 200)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 3100
    assert second.total_cents == 3000


def test_billed_event_id_stays_idempotent(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 100)


def test_plan_change_splits_the_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_subscription("s1").plan_changes == []


def test_plan_change_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 3001))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1500, 1501]


def test_exact_half_segment_share_rounds_up(billing, store):
    store.add_plan(Plan("tiny", "Tiny", 1))
    store.add_plan(Plan("tiny2", "Tiny2", 1))
    store.get_subscription("s1").plan_id = "tiny"
    billing.change_plan("s1", "tiny2", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1, 1]


def test_usage_is_attributed_to_the_segment_of_its_date(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 20, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
    ]


def test_included_units_are_prorated_with_floor(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 11, date(2026, 1, 3))
    billing.record_usage("s1", "b", 61, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 5),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 30, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    kinds = [(item.kind, item.description) for item in invoice.line_items]
    assert kinds == [
        ("plan", "Plan: Basic"),
        ("plan", "Plan: Pro"),
        ("overage", "Overage: Basic"),
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


def test_invalid_changes_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    bad_calls = [
        ("basic", date(2026, 1, 1)),
        ("basic", date(2025, 12, 31)),
        ("basic", date(2026, 1, 31)),
        ("basic", date(2026, 2, 5)),
        ("basic", date(2026, 1, 11)),
        ("basic", date(2026, 1, 5)),
        ("pro", date(2026, 1, 20)),
    ]
    for plan_id, effective_on in bad_calls:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert subscription.plan_changes == before
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_changes_are_cleared_after_invoice(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    from decimal import Decimal

    from ledger.models import DiscountCode

    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -320),
        ("credit", -500),
        ("tax", 238),
    ]
    assert invoice.total_cents == 2618
    assert customer.credit_balance_cents == 0
