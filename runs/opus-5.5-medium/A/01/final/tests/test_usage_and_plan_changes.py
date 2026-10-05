from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture
def store(store):
    # Period is 2026-01-01 .. 2026-01-31: 30 days.
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
            monthly_price_cents=9000,
            included_units=300,
            overage_unit_price_cents=2,
        )
    )
    store.get_subscription("s1").plan_id = "metered"
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_is_idempotent(billing):
    assert billing.record_usage("s1", "e1", 10, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False


def test_record_usage_rejects_bad_input(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    # The rejected calls recorded nothing.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_overage_is_billed(billing):
    billing.record_usage("s1", "e1", 80, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))
    billing.record_usage("s1", "e2", 1000, date(2026, 1, 30))  # duplicate, ignored

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]
    assert invoice.total_cents == 3100


def test_future_usage_waits_and_late_usage_goes_to_open_period(billing):
    billing.record_usage("s1", "future", 110, date(2026, 1, 31))  # == period_end

    first = billing.generate_invoice("s1")
    assert lines(first) == [("plan", "Plan: Metered", 3000)]

    billing.record_usage("s1", "late", 5, date(2026, 1, 10))
    second = billing.generate_invoice("s1")
    assert lines(second) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 75),
    ]

    third = billing.generate_invoice("s1")
    assert lines(third) == [("plan", "Plan: Metered", 3000)]


def test_mid_cycle_plan_change_prorates_and_splits_usage(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days metered, 20 pro
    billing.record_usage("s1", "late", 10, date(2025, 12, 20))  # first segment
    billing.record_usage("s1", "a", 30, date(2026, 1, 10))  # metered, included 33
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))  # pro, included 200

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 6000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Pro", 100),
    ]
    assert invoice.total_cents == 7135

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)


def test_multiple_changes_and_rounding(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    billing.change_plan("s1", "odd", date(2026, 1, 2))  # 1 day metered
    billing.change_plan("s1", "metered", date(2026, 1, 3))  # 1 day odd, 28 metered

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 100),
        ("plan", "Plan: Odd", 33),  # 33.3667 -> 33
        ("plan", "Plan: Metered", 2800),
    ]
    assert store.get_subscription("s1").plan_id == "metered"


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="half", name="Half", monthly_price_cents=45))
    billing.change_plan("s1", "half", date(2026, 1, 21))  # 10 days of 45 -> 15

    store.add_plan(Plan(plan_id="h2", name="H2", monthly_price_cents=3))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[1] == ("plan", "Plan: Half", 15)

    # 3 * 15 / 30 = 1.5 -> 2
    billing.change_plan("s1", "h2", date(2026, 2, 15))
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[1] == ("plan", "Plan: H2", 2)


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2026, 1, 31), date(2025, 12, 31), date(2026, 2, 5)],
)
def test_change_outside_open_period_is_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_invalid_changes_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))  # not after previous
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))  # already current
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    subscription = store.get_subscription("s1")
    assert [c.effective_on for c in subscription.plan_changes] == [date(2026, 1, 10)]
    assert subscription.plan_id == "metered"


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 10))


def test_discount_credit_and_tax_apply_to_usage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 120, date(2026, 1, 5))  # 100 overage

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 100),
        ("discount", -310),
        ("credit", -500),
        ("tax", 229),
    ]
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0
