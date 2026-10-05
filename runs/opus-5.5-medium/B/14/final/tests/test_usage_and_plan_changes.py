from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=100,
            overage_unit_price_cents=5,
        )
    )
    store.add_plan(
        Plan(
            plan_id="big",
            name="Big",
            monthly_price_cents=9000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="tiny", name="Tiny", monthly_price_cents=3))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_has_usage_defaults():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent(billing, store):
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "e1", 120, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e2", -3, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_included_units_has_no_overage(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")
    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_on_period_end_waits_for_next_invoice(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 110, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Metered", 50)


def test_event_is_billed_only_once(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 110, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 3050
    assert second.total_cents == 3000


def test_late_event_billed_on_open_period_first_segment(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 15))
    billing.change_plan("s1", "basic", date(2026, 2, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Basic", 2000),
        ("overage", "Overage: Metered", 35),
    ]


def test_upgrade_mid_period_is_prorated(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, store):
    store.get_subscription("s1").plan_id = "tiny"
    billing.change_plan("s1", "basic", date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Tiny", 1),
        ("plan", "Plan: Basic", 2500),
    ]


def test_multiple_changes_and_overage_per_segment(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "big", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    billing.record_usage("s1", "b", 150, date(2026, 1, 11))
    billing.record_usage("s1", "c", 20, date(2026, 1, 21))
    billing.record_usage("s1", "d", 10, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Big", 3000),
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Big", 100),
    ]
    assert invoice.total_cents == 5135


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 15))
    subscription = store.get_subscription("s1")
    before = list(subscription.plan_changes)

    for plan_id, effective_on in [
        ("basic", date(2026, 1, 1)),
        ("basic", date(2026, 1, 31)),
        ("basic", date(2025, 12, 1)),
        ("basic", date(2026, 1, 15)),
        ("basic", date(2026, 1, 10)),
        ("pro", date(2026, 1, 20)),
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)

    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert subscription.plan_changes == before
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))


def test_plan_after_invoice_is_last_plan_and_changes_cleared(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice
