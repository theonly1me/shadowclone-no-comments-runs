from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def plans(store):
    # The fixture period is 2026-01-01 to 2026-01-31: 30 days.
    store.add_plan(Plan(plan_id="pro", name="Pro", monthly_price_cents=6000))
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=100,
            overage_unit_price_cents=2,
        )
    )
    store.add_plan(
        Plan(
            plan_id="metered_pro",
            name="Metered Pro",
            monthly_price_cents=6000,
            included_units=50,
            overage_unit_price_cents=5,
        )
    )


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def use_metered(store):
    store.get_subscription("s1").plan_id = "metered"


def test_plan_defaults_have_no_usage_pricing():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# Usage events


def test_usage_within_included_units_adds_no_overage(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 100, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_above_included_units_is_billed_as_overage(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 70, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 50, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 40),
    ]
    assert invoice.total_cents == 3040


def test_record_usage_returns_true_then_false_for_duplicate(billing, store):
    use_metered(store)
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 100)


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_non_positive_units_are_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # Nothing was recorded, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_usage_for_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_event_on_period_end_waits_for_the_next_invoice(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 130, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Metered", 60)


def test_usage_is_billed_only_once(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 130, date(2026, 1, 15))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[-1] == ("overage", "Overage: Metered", 60)
    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_the_open_period(billing, store):
    use_metered(store)
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 110, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert invoice.period_start == date(2026, 1, 31)
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 20)


def test_late_event_is_attributed_to_the_first_segment(billing, store):
    use_metered(store)
    billing.generate_invoice("s1")  # Next period: 2026-01-31 to 2026-03-02.
    billing.change_plan("s1", "metered_pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    # Metered segment is 10 of 30 days: 33 included units, 17 overage at 2.
    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 34)


# Plan changes


def test_plan_change_prorates_by_days(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1005))
    billing.change_plan("s1", "odd", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    # 1005 * 29 / 30 = 971.5
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 100),
        ("plan", "Plan: Odd", 972),
    ]


def test_several_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]


def test_usage_is_split_across_segments_with_prorated_allowance(billing, store):
    use_metered(store)
    billing.change_plan("s1", "metered_pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 40, date(2026, 1, 10))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    # Metered: 10 days, 100 * 10 // 30 = 33 included, 7 over at 2.
    # Metered Pro: 20 days, 50 * 20 // 30 = 33 included, 7 over at 5.
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Metered Pro", 4000),
        ("overage", "Overage: Metered", 14),
        ("overage", "Overage: Metered Pro", 35),
    ]
    assert invoice.total_cents == 5049


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store):
    use_metered(store)
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 600, date(2026, 1, 5))

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


def test_invoice_moves_subscription_to_final_plan_and_clears_changes(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


def test_change_does_not_alter_plan_id_before_invoicing(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_change_outside_the_open_period_is_rejected(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


@pytest.mark.parametrize("effective_on", [date(2026, 1, 5), date(2026, 1, 11)])
def test_change_not_after_previous_change_is_rejected(billing, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", effective_on)


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 11))


def test_change_on_unknown_subscription_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_rejected_change_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 31))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
