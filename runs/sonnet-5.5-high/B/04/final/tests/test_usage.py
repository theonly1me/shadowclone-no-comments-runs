from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan(
            "metered",
            "Metered",
            3000,
            included_units=100,
            overage_unit_price_cents=10,
        )
    )
    store.add_plan(Plan("pro", "Pro", 6000, included_units=30, overage_unit_price_cents=20))


def use_plan(store, plan_id):
    store.get_subscription("s1").plan_id = plan_id


def kinds(invoice):
    return [(item.kind, item.amount_cents) for item in invoice.line_items]


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 50, date(2026, 1, 9)) is False


def test_duplicate_event_is_ignored_completely(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 150, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 5, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", 3000), ("overage", 500)]


def test_event_ids_are_scoped_per_subscription(billing, store):
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
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


def test_usage_within_allowance_has_no_overage(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    assert kinds(billing.generate_invoice("s1")) == [("plan", 3000)]


def test_overage_line(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 70, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 60, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", 3000), ("overage", 300)]
    assert invoice.line_items[1].description == "Overage: Metered"
    assert invoice.total_cents == 3300


def test_event_at_period_end_is_not_billed_yet(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 500, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds(first) == [("plan", 3000)]
    assert kinds(second) == [("plan", 3000), ("overage", (500 - 100) * 10)]


def test_event_is_billed_only_once(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert kinds(second) == [("plan", 3000)]


def test_late_event_is_billed_on_open_period(billing, store):
    use_plan(store, "metered")
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 150, date(2025, 12, 20))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", 3000), ("overage", 500)]


def test_event_in_far_future_is_not_billed(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 500, date(2026, 6, 1))

    assert kinds(billing.generate_invoice("s1")) == [("plan", 3000)]


def test_change_plan_prorates_segments(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", 1000), ("plan", 4000)]
    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
    ]
    assert invoice.total_cents == 5000


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1002))
    use_plan(store, "odd")
    store.get_subscription("s1").period_end = date(2026, 1, 5)
    billing.change_plan("s1", "basic", date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", 251), ("plan", 2250)]


def test_plan_after_period_end_is_the_final_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []
    next_invoice = billing.generate_invoice("s1")
    assert kinds(next_invoice) == [("plan", 3000)]


def test_three_segments_in_order(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "metered", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [("plan", 1000), ("plan", 2000), ("plan", 1000)]


def test_change_back_to_original_plan(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]


def test_usage_attributed_to_segment_with_prorated_allowance(billing, store):
    use_plan(store, "metered")
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 50, date(2026, 1, 3))
    billing.record_usage("s1", "b", 20, date(2026, 1, 10))
    billing.record_usage("s1", "c", 25, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", (70 - 33) * 10),
        ("overage", (25 - 20) * 20),
    ]
    assert [item.description for item in invoice.line_items[2:]] == [
        "Overage: Metered",
        "Overage: Pro",
    ]


def test_late_event_goes_to_first_segment(billing, store):
    use_plan(store, "metered")
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", date(2026, 2, 10))
    billing.record_usage("s1", "late", 100, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", (100 - 33) * 10),
    ]


def test_overage_lines_follow_all_plan_lines_then_adjustments(billing, store):
    use_plan(store, "metered")
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "a", 100, date(2026, 1, 2))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 100

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert kinds(invoice) == [
        ("plan", 1500),
        ("plan", 3000),
        ("overage", 500),
        ("discount", -500),
        ("credit", -100),
        ("tax", 440),
    ]
    assert invoice.total_cents == 4840
    assert customer.credit_balance_cents == 0


def test_change_plan_validation_leaves_state_unchanged(billing, store):
    subscription = store.get_subscription("s1")
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    bad_calls = [
        ("pro", date(2026, 1, 20), ValueError),
        ("metered", date(2026, 1, 11), ValueError),
        ("metered", date(2026, 1, 5), ValueError),
        ("metered", date(2026, 1, 1), ValueError),
        ("metered", date(2026, 1, 31), ValueError),
        ("metered", date(2026, 2, 15), ValueError),
        ("ghost", date(2026, 1, 20), KeyError),
    ]
    for plan_id, effective_on, error in bad_calls:
        with pytest.raises(error):
            billing.change_plan("s1", plan_id, effective_on)

    assert subscription.plan_changes == [(date(2026, 1, 11), "pro")]
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_change_plan_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_changes_do_not_carry_into_next_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    billing.change_plan("s1", "basic", date(2026, 2, 5))
    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Pro",
        "Plan: Basic",
    ]
