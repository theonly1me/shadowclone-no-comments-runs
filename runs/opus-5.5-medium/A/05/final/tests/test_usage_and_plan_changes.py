from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription

# The fixture period is [2026-01-01, 2026-01-31): 30 days on "basic" (3000).


@pytest.fixture(autouse=True)
def usage_plans(store):
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=1000,
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


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_defaults_to_no_usage_terms():
    plan = Plan(plan_id="x", name="X", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_duplicate_event_is_ignored(billing, store):
    store.get_subscription("s1").plan_id = "metered"

    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 6)) is False

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 250),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 60, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]
    assert invoice.total_cents == 1000


def test_event_on_period_end_waits_for_next_invoice(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 120, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 100) in lines(second)


def test_event_is_billed_only_once(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 120, date(2026, 1, 10))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.total_cents == 1100
    assert second.total_cents == 1000


def test_late_event_is_billed_on_open_period(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")  # now open: [2026-01-31, 2026-03-02)

    billing.record_usage("s1", "late", 130, date(2026, 1, 15))
    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 150) in lines(invoice)


def test_mid_cycle_upgrade_prorates_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    # 10 days of Basic and 20 days of Pro.
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1001))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # 1001 * 15 / 30 = 500.5 -> 501
    assert lines(invoice)[0] == ("plan", "Plan: Odd", 501)


def test_usage_is_attributed_to_segments(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 70, date(2026, 1, 15))  # Metered, 50 included
    billing.record_usage("s1", "b", 200, date(2026, 1, 16))  # Pro, 150 included
    billing.record_usage("s1", "c", 10, date(2026, 1, 30))  # Pro

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Metered", 100),
        ("overage", "Overage: Pro", 120),
    ]
    assert invoice.total_cents == 3720


def test_included_units_are_floored_per_segment(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    store.get_plan("metered").included_units = 10
    billing.change_plan("s1", "basic", date(2026, 1, 8))
    # 10 * 7 / 30 = 2.33 -> 2 included.
    billing.record_usage("s1", "a", 3, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 5) in lines(invoice)


def test_late_event_counts_toward_first_segment(billing, store):
    billing.generate_invoice("s1")  # now open: [2026-01-31, 2026-03-02), 30 days
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", date(2026, 2, 15))
    billing.record_usage("s1", "late", 60, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    # Metered runs 15 days: 50 included, 10 over.
    assert ("overage", "Overage: Metered", 50) in lines(invoice)
    assert not any(desc == "Overage: Pro" for _, desc, _ in lines(invoice))


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 333),
    ]


def test_after_invoice_plan_is_final_and_changes_cleared(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]


@pytest.mark.parametrize(
    "effective_on",
    [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)],
)
def test_change_outside_open_period_is_rejected(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


def test_change_must_follow_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")
    assert [desc for _, desc, _ in lines(invoice)] == ["Plan: Basic", "Plan: Pro"]


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))
    # Changing back to the original plan is fine.
    billing.change_plan("s1", "basic", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))

    assert billing.generate_invoice("s1").total_cents == 3000
    assert store.get_subscription("s1").plan_id == "basic"


def test_discount_credit_and_tax_apply_to_usage_subtotal(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 100
    billing.record_usage("s1", "e1", 300, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(kind, amount) for kind, _, amount in lines(invoice)] == [
        ("plan", 1000),
        ("overage", 1000),
        ("discount", -200),
        ("credit", -100),
        ("tax", 170),
    ]
    assert invoice.total_cents == 1870
    assert customer.credit_balance_cents == 0
