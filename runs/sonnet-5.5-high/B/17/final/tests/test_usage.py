from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def metered_plans(store):
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
    store.get_subscription("s1").plan_id = "metered"


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 9)) is False


def test_duplicate_event_is_ignored_completely(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 9))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Metered", 100)


def test_same_event_id_on_different_subscriptions_is_distinct(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


@pytest.mark.parametrize("units", [0, -3])
def test_non_positive_units_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_unknown_subscription_in_record_usage(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 10, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 50),
    ]
    assert invoice.total_cents == 3050


def test_event_at_period_end_is_billed_on_next_invoice(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert [item.kind for item in second.line_items] == ["plan", "overage"]


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert first.line_items[1].amount_cents == 100
    assert [item.kind for item in second.line_items] == ["plan"]


def test_billed_event_id_stays_deduplicated(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 2, 5)) is False
    assert [item.kind for item in billing.generate_invoice("s1").line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Metered", 100)


def test_change_plan_prorates_plan_lines(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_plan_proration_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1001))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "pro", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [501, 3000]


def test_usage_attributed_to_segment_and_included_is_prorated(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 20, date(2026, 1, 10))
    billing.record_usage("s1", "e2", 500, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 100),
        ("overage", "Overage: Pro", 0),
    ]


def test_included_units_use_floor_division(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 3))
    billing.record_usage("s1", "e1", 3, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert invoice.line_items[2].amount_cents == 10


def test_late_event_goes_to_first_segment(billing):
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 100, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[2][1:] == ("Overage: Metered", 900)


def test_overage_lines_follow_all_plan_lines_chronologically(billing, store):
    store.add_plan(Plan("m2", "Metered2", 3000, 0, 7))
    store.get_subscription("s1").plan_id = "m2"
    billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 21))
    billing.record_usage("s1", "a", 2, date(2026, 1, 3))
    billing.record_usage("s1", "b", 100, date(2026, 1, 15))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description) for item in invoice.line_items] == [
        ("plan", "Plan: Metered2"),
        ("plan", "Plan: Metered"),
        ("plan", "Plan: Pro"),
        ("overage", "Overage: Metered2"),
        ("overage", "Overage: Metered"),
    ]
    assert [item.amount_cents for item in invoice.line_items[3:]] == [14, 900]


def test_discount_credit_tax_apply_to_subtotal_with_overage(billing, store):
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


def test_invoice_end_state_after_changes(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)

    invoice = billing.generate_invoice("s1")
    assert [item.description for item in invoice.line_items] == ["Plan: Metered"]


def test_change_plan_can_return_to_an_earlier_plan(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Metered",
        "Plan: Pro",
        "Plan: Metered",
    ]


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)]
)
def test_change_outside_period_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_not_after_previous_rejected_and_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for day in (date(2026, 1, 11), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "metered", day)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 15))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_unknown_plan_or_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_changes_apply_to_the_new_period_after_invoice(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    billing.change_plan("s1", "metered", date(2026, 2, 1))
    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Pro",
        "Plan: Metered",
    ]
    assert [item.amount_cents for item in invoice.line_items] == [
        round(6000 * 1 / 30),
        round(3000 * 29 / 30),
    ]
