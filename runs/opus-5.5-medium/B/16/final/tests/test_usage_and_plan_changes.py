from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def plans(store):
    basic = store.get_plan("basic")
    basic.included_units = 100
    basic.overage_unit_price_cents = 5
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_within_allowance_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Basic", 3000)]


def test_usage_over_allowance_is_billed(billing):
    billing.record_usage("s1", "e1", 120, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 30, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 250),
    ]
    assert invoice.total_cents == 3250


def test_record_usage_is_idempotent_by_event_id(billing):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Basic", 250)


def test_event_ids_are_scoped_per_subscription(billing, store):

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_duplicate_is_rejected_even_after_billing(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 5)) is False


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_are_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))


def test_usage_for_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_event_on_or_after_period_end_waits_for_next_invoice(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Basic", 250)


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 150, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Basic", 250)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_plan_change_prorates_by_days(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up_per_segment(billing, store):
    store.get_plan("odd").monthly_price_cents = 1005
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 7))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Odd", 201),
        ("plan", "Plan: Basic", 2400),
    ]


def test_multiple_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "odd", date(2026, 1, 21))
    billing.change_plan("s1", "basic", date(2026, 1, 28))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Odd", 233),
        ("plan", "Plan: Basic", 300),
    ]


def test_usage_is_attributed_to_segments(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 35),
        ("overage", "Overage: Pro", 100),
    ]


def test_late_event_is_attributed_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 40, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 35),
    ]


def test_discount_credit_and_tax_apply_after_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(kind, amount) for kind, _, amount in lines(invoice)] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_invoice_moves_to_final_plan_and_clears_changes(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_not_after_previous_change_is_rejected(billing, store, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "odd", effective_on)

    changes = store.get_subscription("s1").plan_changes
    assert [(c.plan_id, c.effective_on) for c in changes] == [("pro", date(2026, 1, 10))]


def test_change_to_current_plan_is_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))

    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_back_to_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [amount for _, _, amount in lines(invoice)] == [1000, 2000, 1000]


def test_change_to_unknown_plan_raises(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_change_for_unknown_subscription_raises(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
