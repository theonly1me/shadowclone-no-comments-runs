from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
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


def kinds(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false_for_duplicates(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 3)) is False


def test_duplicate_event_is_ignored_completely(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 35, date(2026, 1, 2))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Metered", 50)


def test_same_event_id_on_different_subscriptions_is_independent(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 2)) is True


def test_record_usage_rejects_non_positive_units(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 2))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 2))


def test_usage_within_included_units_has_no_overage(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 30, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_overage_is_billed_once(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 40, date(2026, 1, 2))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds(first)[1] == ("overage", "Overage: Metered", 100)
    assert [item.kind for item in second.line_items] == ["plan"]


def test_event_at_period_end_is_not_billed_yet(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.record_usage("s1", "e1", 100, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert kinds(second)[1] == ("overage", "Overage: Metered", 700)


def test_late_event_is_billed_on_open_period(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 31, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[1] == ("overage", "Overage: Metered", 10)


def test_plan_change_splits_the_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_plan_lines_use_half_up_rounding(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1005))
    store.get_subscription("s1").plan_id = "odd"
    store.add_plan(Plan(plan_id="z", name="Z", monthly_price_cents=0))
    billing.change_plan("s1", "z", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[0] == ("plan", "Plan: Odd", 503)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_usage_attributed_to_segments_with_prorated_allowance(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))
    billing.record_usage("s1", "a", 20, date(2026, 1, 10))
    billing.record_usage("s1", "b", 500, date(2026, 1, 15))
    billing.record_usage("s1", "c", 11, date(2026, 1, 21))
    billing.record_usage("s1", "d", 1, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
        ("overage", "Overage: Metered", 100),
        ("overage", "Overage: Pro", 0),
        ("overage", "Overage: Metered", 20),
    ]


def test_late_event_attributed_to_first_segment(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 50, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[2] == ("overage", "Overage: Metered", 400)


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.get_subscription("s1").plan_id = "metered"
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 40, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert kinds(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
        ("discount", "Discount", -310),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 229),
    ]
    assert invoice.total_cents == 3000 + 100 - 310 - 500 + 229
    assert customer.credit_balance_cents == 0


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    subscription = store.get_subscription("s1")
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    bad = [
        ("pro", date(2026, 1, 21)),
        ("basic", date(2026, 1, 1)),
        ("basic", date(2026, 1, 31)),
        ("basic", date(2026, 2, 5)),
        ("basic", date(2026, 1, 11)),
        ("basic", date(2026, 1, 5)),
    ]
    for plan_id, effective_on in bad:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 21))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 21))

    assert [(c.plan_id, c.effective_on) for c in subscription.plan_changes] == [
        ("pro", date(2026, 1, 11))
    ]
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_changes_reset_after_invoice(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    billing.change_plan("s1", "basic", date(2026, 2, 1))
    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Pro",
        "Plan: Basic",
    ]
