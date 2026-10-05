from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

# The fixture period is [2026-01-01, 2026-01-31): 30 days on "basic" (3000).


@pytest.fixture(autouse=True)
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
    store.add_plan(Plan(plan_id="tiny", name="Tiny", monthly_price_cents=15))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def use_metered(store):
    store.get_subscription("s1").plan_id = "metered"


def test_plan_usage_fields_default_to_zero():
    plan = Plan(plan_id="p", name="P", monthly_price_cents=100)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


# record_usage


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is True
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 3)) is False


def test_duplicate_event_is_ignored_even_with_different_data(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 150, date(2026, 1, 3))
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 4)) is False

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 250) in lines(invoice)


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 3)) is True


@pytest.mark.parametrize("units", [0, -1])
def test_record_usage_rejects_non_positive_units(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 3))
    # Nothing was stored, so the id is still free.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 3)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 3))


# Usage billing


def test_usage_within_allowance_adds_no_overage(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 100, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_over_allowance_adds_overage_line(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 70, date(2026, 1, 1))
    billing.record_usage("s1", "e2", 50, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]
    assert invoice.total_cents == 3100


def test_event_on_period_end_waits_for_next_invoice(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 150, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert ("overage", "Overage: Metered", 250) in lines(second)


def test_events_are_billed_only_once(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 150, date(2026, 1, 3))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, store):
    use_metered(store)
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 130, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 150) in lines(invoice)


def test_late_event_goes_to_first_segment(billing, store):
    use_metered(store)
    billing.generate_invoice("s1")  # open period is now [01-31, 03-02), 30 days
    billing.record_usage("s1", "late", 110, date(2026, 1, 15))
    billing.change_plan("s1", "pro", date(2026, 2, 15))  # 15 days each

    invoice = billing.generate_invoice("s1")

    # Metered segment allows 100 * 15 // 30 = 50 units, so 60 over at 5.
    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Metered", 300),
    ]


# change_plan


def test_change_plan_prorates_by_days(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_rounds_each_segment_half_up(billing):
    billing.change_plan("s1", "tiny", date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    # Basic: 3000 * 29 / 30 = 2900. Tiny: 15 * 1 / 30 = 0.5 -> 1.
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 2900),
        ("plan", "Plan: Tiny", 1),
    ]


def test_multiple_changes_and_usage_per_segment(billing, store):
    use_metered(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))
    # Segments are 10 days each. Metered allows 33, pro allows 100.
    billing.record_usage("s1", "a", 40, date(2026, 1, 1))
    billing.record_usage("s1", "b", 100, date(2026, 1, 11))
    billing.record_usage("s1", "c", 34, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 35),
        ("overage", "Overage: Metered", 5),
    ]
    assert invoice.total_cents == 4040


def test_usage_on_change_day_belongs_to_new_plan(billing, store):
    billing.change_plan("s1", "metered", date(2026, 1, 16))
    billing.record_usage("s1", "a", 60, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # Metered allows 100 * 15 // 30 = 50.
    assert ("overage", "Overage: Metered", 50) in lines(invoice)


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31)]
)
def test_change_must_be_inside_the_period(billing, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)


@pytest.mark.parametrize("effective_on", [date(2026, 1, 10), date(2026, 1, 5)])
def test_change_must_be_after_previous_change(billing, effective_on):
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", effective_on)


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    billing.change_plan("s1", "pro", date(2026, 1, 10))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))


def test_change_to_unknown_plan_or_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))


def test_rejected_change_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for plan_id, effective_on in [
        ("metered", date(2026, 1, 5)),
        ("pro", date(2026, 1, 20)),
        ("missing", date(2026, 1, 20)),
    ]:
        with pytest.raises((ValueError, KeyError)):
            billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1").plan_id == "basic"
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1000, 4000]


def test_after_invoice_plan_is_final_plan_and_changes_are_cleared(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert (subscription.period_start, subscription.period_end) == (
        date(2026, 1, 31),
        date(2026, 3, 2),
    )

    invoice = billing.generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Pro", 6000)]

    # A change early in the new period is allowed again.
    billing.change_plan("s1", "basic", date(2026, 3, 3))


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    use_metered(store)
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 300, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # Subtotal 3000 + 200 * 5 = 4000.
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
