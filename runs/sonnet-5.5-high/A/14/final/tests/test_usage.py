from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    # Period is 2026-01-01 .. 2026-01-31, i.e. 30 days.
    store.add_plan(
        Plan("metered", "Metered", 3000, included_units=100, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=5)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def use_plan(store, plan_id):
    store.get_subscription("s1").plan_id = plan_id


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -3, date(2026, 1, 5))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))
    # Rejected calls did not consume the event id.
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_duplicate_event_is_ignored_completely(billing, store):
    use_plan(store, "metered")
    assert billing.record_usage("s1", "e1", 150, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 9999, date(2026, 2, 20)) is False

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Metered", 50 * 10)


def test_event_ids_are_scoped_per_subscription(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_usage_within_allowance_has_no_overage_line(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_usage_is_summed_and_billed_once(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 80, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 40, date(2026, 1, 30))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first)[1] == ("overage", "Overage: Metered", 200)
    assert lines(second) == [("plan", "Plan: Metered", 3000)]


def test_event_at_period_end_is_not_billed_yet(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "future", 500, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")

    assert lines(first) == [("plan", "Plan: Metered", 3000)]
    assert lines(second)[1] == ("overage", "Overage: Metered", 400 * 10)
    assert billing.record_usage("s1", "future", 1, date(2026, 1, 31)) is False


def test_late_event_is_billed_on_the_open_period(billing, store):
    use_plan(store, "metered")
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 130, date(2025, 12, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Metered", 300)


def test_plan_change_splits_the_period(billing, store):
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
    assert subscription.period_start == date(2026, 1, 31)

    # The next period is billed entirely on the new plan.
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 6000)]


def test_segment_amounts_round_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 3005))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # 3005 * 15 / 30 = 1502.5 -> 1503
    assert lines(invoice) == [
        ("plan", "Plan: Odd", 1503),
        ("plan", "Plan: Basic", 1500),
    ]


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("pro", date(2026, 1, 1)),  # not after period_start
        ("pro", date(2025, 12, 20)),
        ("pro", date(2026, 1, 31)),  # not before period_end
        ("pro", date(2026, 2, 5)),
        ("basic", date(2026, 1, 10)),  # already current
    ],
)
def test_invalid_plan_changes_are_rejected(billing, store, plan_id, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", plan_id, effective_on)

    assert store.get_subscription("s1").plan_changes == []
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_change_must_be_after_previous_change_and_to_a_different_plan(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 5))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 20))  # already pro

    assert len(store.get_subscription("s1").plan_changes) == 1


def test_changing_back_to_the_original_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 12))


def test_unknown_plan_and_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


def test_usage_is_attributed_to_segments_with_prorated_allowance(billing, store):
    use_plan(store, "metered")  # 100 units / 30 days
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 300 units / 30 days
    # metered: 10 days -> included floor(100*10/30)=33; pro: 20 days -> 200
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))  # metered, 7 over
    billing.record_usage("s1", "b", 250, date(2026, 1, 11))  # pro, 50 over
    billing.record_usage("s1", "c", 10, date(2025, 12, 1))  # late -> metered, 17 over

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 17 * 10),
        ("overage", "Overage: Pro", 50 * 5),
    ]
    assert invoice.total_cents == 5000 + 170 + 250


def test_discount_credit_and_tax_apply_to_plan_and_overage(billing, store):
    use_plan(store, "metered")
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))  # 100 over = 1000

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 4000, discount 400, credit 500, taxable 3100, tax 310
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 3000),
        ("overage", 1000),
        ("discount", -400),
        ("credit", -500),
        ("tax", 310),
    ]
    assert invoice.total_cents == 3410
    assert customer.credit_balance_cents == 0


def test_failed_invoice_leaves_state_unchanged(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 200, date(2026, 1, 5))
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="MISSING")

    subscription = store.get_subscription("s1")
    assert subscription.period_start == date(2026, 1, 1)
    assert len(subscription.plan_changes) == 1
    invoice = billing.generate_invoice("s1")
    assert lines(invoice)[2] == ("overage", "Overage: Metered", 167 * 10)
