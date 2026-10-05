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
    store.add_plan(Plan(plan_id="max", name="Max", monthly_price_cents=9000))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def use_metered(store):
    store.get_subscription("s1").plan_id = "metered"


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 9)) is False


def test_duplicate_event_is_ignored_completely(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 40, date(2026, 1, 2))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 100)


def test_same_event_id_on_other_subscription_is_distinct(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 2)) is True


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 2))
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True


def test_usage_within_included_units_has_no_overage(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 30, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert [item.kind for item in invoice.line_items] == ["plan"]


def test_overage_is_billed(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 25, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 15, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 100),
    ]
    assert invoice.total_cents == 3100


def test_event_is_billed_only_once(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 50, date(2026, 1, 2))

    billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in second.line_items] == ["plan"]


def test_event_at_period_end_is_not_billed_yet(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 50, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert [item.kind for item in first.line_items] == ["plan"]
    assert lines(second)[-1] == ("overage", "Overage: Metered", 200)


def test_late_event_is_billed_on_open_period(billing, store):
    use_metered(store)
    billing.record_usage("s1", "e1", 50, date(2025, 12, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 200)


def test_change_plan_splits_the_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_plan_segments_round_half_up(billing, store):
    store.add_plan(Plan(plan_id="odd", name="Odd", monthly_price_cents=1005))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[0] == ("plan", "Plan: Odd", 503)
    assert lines(invoice)[1] == ("plan", "Plan: Basic", 1500)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "max", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Max", 3000),
    ]
    assert store.get_subscription("s1").plan_id == "max"


def test_change_back_to_original_plan_in_same_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert [item.description for item in invoice.line_items] == [
        "Plan: Basic",
        "Plan: Pro",
        "Plan: Basic",
    ]
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),
        ("pro", date(2025, 12, 31)),
        ("pro", date(2026, 1, 31)),
        ("pro", date(2026, 2, 5)),
        ("basic", date(2026, 1, 10)),
    ],
)
def test_invalid_change_leaves_state_unchanged(billing, store, plan_id, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1").plan_changes == []


def test_change_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "max", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "max", date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_unknown_plan_raises_key_error(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_unknown_subscription_change_raises_key_error(billing):
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))


def test_usage_is_attributed_to_segment_containing_it(billing, store):
    use_metered(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 20, date(2026, 1, 10))
    billing.record_usage("s1", "e2", 500, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 100),
        ("overage", "Overage: Pro", 0),
    ]


def test_late_event_goes_to_first_segment(billing, store):
    use_metered(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 20, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 100)


def test_included_units_are_prorated_with_floor(billing, store):
    use_metered(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 11, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[-1] == ("overage", "Overage: Metered", 10)


def test_overage_lines_follow_all_plan_lines_in_order(billing, store):
    store.add_plan(
        Plan("m2", "Metered2", 3000, included_units=0, overage_unit_price_cents=5)
    )
    use_metered(store)
    billing.change_plan("s1", "m2", date(2026, 1, 16))
    billing.record_usage("s1", "e1", 25, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 4, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1500),
        ("plan", "Plan: Metered2", 1500),
        ("overage", "Overage: Metered", 100),
        ("overage", "Overage: Metered2", 20),
    ]


def test_discount_credit_tax_apply_to_usage_subtotal(billing, store):
    use_metered(store)
    store.add_discount_code(DiscountCode(code="TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 50, date(2026, 1, 2))

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 200),
        ("discount", "Discount", -320),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 238),
    ]
    assert invoice.total_cents == 2618
    assert customer.credit_balance_cents == 0


def test_next_period_after_change_starts_on_new_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    second = billing.generate_invoice("s1")

    assert lines(second) == [("plan", "Plan: Pro", 6000)]
