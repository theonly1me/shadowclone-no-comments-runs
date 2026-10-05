from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture
def plans(store):
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
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(Plan(plan_id="team", name="Team", monthly_price_cents=9000))
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)

    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_rejects_non_positive_units(billing, plans):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_is_idempotent_by_event_id(billing, plans):
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, plans):
    from ledger.models import Subscription

    plans.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )

    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing, plans):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]
    assert invoice.total_cents == 3000


def test_usage_on_or_after_period_end_waits_for_next_invoice(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))
    billing.record_usage("s1", "e2", 10, date(2026, 1, 30))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 3000
    assert lines(second)[-1] == ("overage", "Overage: Metered", 250)


def test_usage_is_billed_only_once(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 3250
    assert second.total_cents == 3000


def test_late_usage_is_billed_on_open_period(billing, plans):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 120, date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 2, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Metered", 350),
    ]


def test_change_plan_prorates_segments(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=45))
    billing.change_plan("s1", "odd", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 100),
        ("plan", "Plan: Odd", 44),
    ]


def test_multiple_changes_with_usage_and_overage(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))
    billing.change_plan("s1", "metered", date(2026, 1, 26))
    billing.record_usage("s1", "a", 40, date(2026, 1, 1))
    billing.record_usage("s1", "b", 10, date(2026, 1, 10))
    billing.record_usage("s1", "c", 150, date(2026, 1, 11))
    billing.record_usage("s1", "d", 7, date(2026, 1, 21))
    billing.record_usage("s1", "e", 20, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Team", 1500),
        ("plan", "Plan: Metered", 500),
        ("overage", "Overage: Metered", 85),
        ("overage", "Overage: Pro", 100),
        ("overage", "Overage: Team", 0),
        ("overage", "Overage: Metered", 20),
    ]
    assert invoice.total_cents == 5205


def test_overage_flows_through_discount_credit_and_tax(billing, plans):
    plans.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = plans.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 500),
        ("discount", -350),
        ("credit", -500),
        ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_invoice_resets_plan_and_changes(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "team", date(2026, 1, 21))

    billing.generate_invoice("s1")
    subscription = plans.get_subscription("s1")

    assert subscription.plan_id == "team"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Team", 9000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_plan_rejects_dates_outside_open_period(billing, plans, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert plans.get_subscription("s1").plan_changes == []


def test_change_plan_requires_increasing_dates(billing, plans):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "team", date(2026, 1, 5))

    assert len(plans.get_subscription("s1").plan_changes) == 1


def test_change_plan_rejects_current_plan(billing, plans):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))

    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    billing.change_plan("s1", "metered", date(2026, 1, 20))


def test_change_plan_unknown_plan_or_subscription(billing, plans):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert plans.get_subscription("s1").plan_changes == []


def test_failed_invoice_leaves_usage_unbilled(billing, plans):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")

    assert billing.generate_invoice("s1").total_cents == 3250
