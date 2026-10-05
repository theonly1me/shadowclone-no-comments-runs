from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription

D = date


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, D(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, D(2026, 1, 9)) is False


def test_duplicate_is_ignored_completely(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 150, D(2026, 1, 5))
    billing.record_usage("s1", "e1", 999, D(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Metered", 350)


def test_event_id_is_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", D(2026, 1, 1), D(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, D(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, D(2026, 1, 5)) is True


def test_duplicate_id_of_a_billed_event_is_still_ignored(billing):
    billing.record_usage("s1", "e1", 1, D(2026, 1, 5))
    billing.generate_invoice("s1")
    assert billing.record_usage("s1", "e1", 1, D(2026, 2, 5)) is False


@pytest.mark.parametrize("units", [0, -3])
def test_non_positive_units_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, D(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, D(2026, 1, 5)) is True


def test_unknown_subscription_for_usage(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, D(2026, 1, 5))


def test_overage_line_for_single_segment(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 60, D(2026, 1, 5))
    billing.record_usage("s1", "e2", 70, D(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 30 * 7),
    ]
    assert invoice.total_cents == 3210


def test_usage_within_included_units_has_no_overage(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 100, D(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_is_not_billed_yet(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 500, D(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert second.line_items[1].amount_cents == 400 * 7


def test_event_is_billed_only_once(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 500, D(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 110, D(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Metered", 70)


def test_change_plan_prorates_by_days(billing, metered):
    billing.change_plan("s1", "pro", D(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = metered.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert subscription.period_start == D(2026, 1, 31)


def test_proration_rounds_half_up(billing, metered):
    metered.add_plan(Plan("odd", "Odd", 1001))
    billing.change_plan("s1", "odd", D(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [1500, 501]


def test_multiple_changes_in_one_period(billing, metered):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.change_plan("s1", "metered", D(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
    ]
    assert metered.get_subscription("s1").plan_id == "metered"


def test_change_back_to_original_plan_is_allowed(billing, metered):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.change_plan("s1", "basic", D(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]


def test_usage_is_attributed_to_segments_with_prorated_allowance(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.record_usage("s1", "a", 50, D(2026, 1, 10))
    billing.record_usage("s1", "b", 400, D(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 17 * 7),
        ("overage", "Overage: Pro", 200 * 5),
    ]


def test_late_event_goes_to_first_segment(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", D(2026, 2, 15))
    billing.record_usage("s1", "late", 200, D(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    overage = [i for i in invoice.line_items if i.kind == "overage"]
    assert [(i.description, i.amount_cents) for i in overage] == [
        ("Overage: Metered", (200 - 100 * 15 // 30) * 7)
    ]


def test_discount_credit_tax_apply_after_overage(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    customer = metered.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    metered.add_discount_code(DiscountCode(code="OFF", amount_off_cents=200))
    billing.record_usage("s1", "e1", 200, D(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="OFF")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 700),
        ("discount", "Discount", -200),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 300),
    ]
    assert invoice.total_cents == 3300
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize(
    "effective_on",
    [D(2026, 1, 1), D(2025, 12, 1), D(2026, 1, 31), D(2026, 3, 1)],
)
def test_effective_on_must_be_inside_period(billing, metered, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert metered.get_subscription("s1").plan_changes == []


def test_changes_must_be_strictly_increasing(billing, metered):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    for day in (D(2026, 1, 11), D(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "metered", day)
    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_rejected(billing, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", D(2026, 1, 11))
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", D(2026, 1, 21))
    assert len(metered.get_subscription("s1").plan_changes) == 1


def test_unknown_plan_and_subscription(billing, metered):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", D(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", D(2026, 1, 11))
    assert metered.get_subscription("s1").plan_changes == []


def test_changes_do_not_leak_into_next_period(billing, metered):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.generate_invoice("s1")

    second = billing.generate_invoice("s1")

    assert kinds(second) == [("plan", "Plan: Pro", 6000)]


def test_failed_invoice_leaves_state_unchanged(billing, metered):
    metered.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 500, D(2026, 1, 5))
    billing.change_plan("s1", "pro", D(2026, 1, 11))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="missing")

    invoice = billing.generate_invoice("s1")
    assert any(i.kind == "overage" for i in invoice.line_items)
