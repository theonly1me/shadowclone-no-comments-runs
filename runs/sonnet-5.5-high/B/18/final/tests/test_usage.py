from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=30, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("metered", "Metered", 3000, included_units=60, overage_unit_price_cents=5)
    )
    store.get_plan("basic").included_units = 10
    store.get_plan("basic").overage_unit_price_cents = 20


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 9)) is False


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_idempotency_is_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_ignored_duplicate_does_not_change_billing(billing):
    billing.record_usage("s1", "e1", 15, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 2, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[-1] == ("overage", "Overage: Basic", 100)


def test_duplicate_of_billed_event_is_still_ignored(billing):
    billing.record_usage("s1", "e1", 15, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 15, date(2026, 2, 5)) is False
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 25, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 300),
    ]
    assert invoice.total_cents == 3300


def test_usage_within_included_has_no_overage(billing):
    billing.record_usage("s1", "e1", 10, date(2026, 1, 5))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_future_event_not_billed_until_its_period(billing):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in first.line_items] == ["plan"]
    assert [i.kind for i in second.line_items] == ["plan", "overage"]


def test_event_billed_only_once(billing):
    billing.record_usage("s1", "e1", 50, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [i.kind for i in second.line_items] == ["plan"]


def test_late_event_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 30, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[-1] == ("overage", "Overage: Basic", 400)


def test_change_plan_splits_plan_lines(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert store.get_subscription("s1").plan_id == "pro"
    assert store.get_subscription("s1").plan_changes == []


def test_change_plan_rounds_half_up_per_segment(billing, store):
    store.add_plan(Plan("odd", "Odd", 1))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [1500, 1]


def test_usage_attributed_to_segments(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 10, date(2026, 1, 10))
    billing.record_usage("s1", "b", 30, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 7 * 20),
        ("overage", "Overage: Pro", 10 * 10),
    ]


def test_included_units_prorated_with_floor(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 5, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[-1] == ("overage", "Overage: Basic", 2 * 20)


def test_late_event_goes_to_first_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "late", 13, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[-1] == ("overage", "Overage: Basic", 20 * 10)


def test_overage_lines_chronological_after_plan_lines(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 100, date(2026, 1, 12))
    billing.record_usage("s1", "b", 100, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert [i.kind for i in invoice.line_items] == ["plan", "plan", "overage", "overage"]
    assert [i.description for i in invoice.line_items[2:]] == [
        "Overage: Basic",
        "Overage: Pro",
    ]


def test_multiple_changes(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [i.amount_cents for i in invoice.line_items] == [1000, 2000, 1000]
    assert store.get_subscription("s1").plan_id == "metered"


def test_change_plan_back_to_original(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [i.description for i in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]


@pytest.mark.parametrize(
    "effective_on", [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)]
)
def test_change_plan_outside_period_rejected(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_change_plan_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "metered", date(2026, 1, 5))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 15))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_plan_unknown_plan_and_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_new_period_starts_on_new_plan(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", "Plan: Pro", 6000)]
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 2, 10))


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 200),
        ("discount", -320),
        ("credit", -500),
        ("tax", 238),
    ]
    assert invoice.total_cents == 2618
    assert customer.credit_balance_cents == 0


def test_unknown_discount_leaves_state_unchanged(billing, store):
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert len(subscription.plan_changes) == 1
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == [
        "plan",
        "plan",
        "overage",
    ]
