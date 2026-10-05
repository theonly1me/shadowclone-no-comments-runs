from datetime import date

import pytest

from ledger.models import Plan


@pytest.fixture
def metered(store):
    store.add_plan(
        Plan(
            plan_id="pro",
            name="Pro",
            monthly_price_cents=6000,
            included_units=30,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(
        Plan(
            plan_id="metered",
            name="Metered",
            monthly_price_cents=3000,
            included_units=10,
            overage_unit_price_cents=25,
        )
    )
    return store


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_completely(billing, store):
    store.add_plan(Plan("m", "M", 1000, overage_unit_price_cents=1))
    store.get_subscription("s1").plan_id = "m"
    billing.record_usage("s1", "e1", 5, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 99, date(2026, 1, 6))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: M", 1000),
        ("overage", "Overage: M", 5),
    ]


def test_same_event_id_on_different_subscriptions_is_independent(billing, store):
    from ledger.models import Subscription

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


def test_overage_is_billed_after_plan_lines(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 8, date(2026, 1, 3))
    billing.record_usage("s1", "e2", 7, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 5 * 25),
    ]
    assert invoice.total_cents == 3125


def test_usage_within_included_units_has_no_overage(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 10, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_event_at_period_end_is_not_billed_yet(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 20, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 10 * 25),
    ]


def test_event_is_billed_only_once(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_event_id_cannot_be_reused_after_billing(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 20, date(2026, 2, 5)) is False
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_late_event_is_billed_on_open_period(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 15, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 5 * 25),
    ]


def test_mid_period_change_splits_plan_lines(billing, store, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_plan_line_rounds_half_up(billing, store, metered):
    store.add_plan(Plan("odd", "Odd", 1001))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [item.amount_cents for item in invoice.line_items] == [1500, 501]


def test_usage_is_attributed_to_segment_by_date(billing, store, metered):
    billing.change_plan("s1", "metered", date(2026, 1, 16))
    billing.record_usage("s1", "a", 4, date(2026, 1, 15))
    billing.record_usage("s1", "b", 9, date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Metered", 1500),
        ("overage", "Overage: Basic", 0),
        ("overage", "Overage: Metered", 9 * 25 - 5 * 25),
    ]


def test_included_units_are_prorated_with_floor(billing, store, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 25, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [("overage", "Overage: Pro", 5 * 10)]


def test_late_event_is_attributed_to_first_segment(billing, store, metered):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 15))
    billing.record_usage("s1", "late", 12, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    kinds = [(item.kind, item.description) for item in invoice.line_items]
    assert kinds == [
        ("plan", "Plan: Metered"),
        ("plan", "Plan: Pro"),
        ("overage", "Overage: Metered"),
    ]
    assert invoice.line_items[2].amount_cents == (12 - 10 * 15 // 30) * 25


def test_multiple_changes_in_one_period(billing, store, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]


def test_overage_lines_follow_all_plan_lines_in_order(billing, store, metered):
    billing.change_plan("s1", "metered", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 21))
    billing.record_usage("s1", "a", 20, date(2026, 1, 15))
    billing.record_usage("s1", "b", 30, date(2026, 1, 25))

    invoice = billing.generate_invoice("s1")

    assert [(item.kind, item.description) for item in invoice.line_items] == [
        ("plan", "Plan: Basic"),
        ("plan", "Plan: Metered"),
        ("plan", "Plan: Pro"),
        ("overage", "Overage: Metered"),
        ("overage", "Overage: Pro"),
    ]


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store, metered):
    from decimal import Decimal

    from ledger.models import DiscountCode

    store.get_subscription("s1").plan_id = "metered"
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "a", 14, date(2026, 1, 5))

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


def test_after_invoice_plan_and_changes_are_updated(billing, store, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    next_invoice = billing.generate_invoice("s1")
    assert lines(next_invoice) == [("plan", "Plan: Metered", 3000)]


def test_change_plan_validation(billing, store, metered):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 1))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 31))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 2, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 10))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 10))
    assert store.get_subscription("s1").plan_changes == []


def test_change_plan_must_be_strictly_after_previous_change(billing, store, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert len(store.get_subscription("s1").plan_changes) == 1
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1000, 4000]


def test_failed_invoice_leaves_state_unchanged(billing, store, metered):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 50, date(2026, 1, 12))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert len(subscription.plan_changes) == 1
    assert subscription.period_start == date(2026, 1, 1)
    invoice = billing.generate_invoice("s1")
    assert invoice.line_items[-1].kind == "overage"
