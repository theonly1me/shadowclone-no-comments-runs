from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan

D = date


@pytest.fixture(autouse=True)
def plans(store):
    # Period is [Jan 1, Jan 31): 30 days.
    store.add_plan(
        Plan("metered", "Metered", 3000, included_units=30, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=20)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def use_plan(store, plan_id):
    store.get_subscription("s1").plan_id = plan_id


# --- record_usage -----------------------------------------------------------


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, D(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 99, D(2026, 1, 9)) is False


def test_duplicate_event_is_ignored_completely(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 35, D(2026, 1, 2))
    billing.record_usage("s1", "e1", 1000, D(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Metered", 50)


def test_duplicate_stays_ignored_after_it_was_billed(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 35, D(2026, 1, 2))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 35, D(2026, 2, 2)) is False
    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_same_event_id_on_different_subscriptions_is_independent(billing, store):
    from ledger.models import Subscription

    store.add_subscription(
        Subscription("s2", "c1", "metered", D(2026, 1, 1), D(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, D(2026, 1, 2)) is True
    assert billing.record_usage("s2", "e1", 1, D(2026, 1, 2)) is True


@pytest.mark.parametrize("units", [0, -3])
def test_non_positive_units_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, D(2026, 1, 2))
    # the rejected call did not consume the event id
    assert billing.record_usage("s1", "e1", 1, D(2026, 1, 2)) is True


def test_unknown_subscription_for_usage(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, D(2026, 1, 2))


# --- usage billing ----------------------------------------------------------


def test_usage_within_included_units_has_no_overage(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 30, D(2026, 1, 5))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [("plan", "Plan: Metered", 3000)]


def test_overage_line(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 20, D(2026, 1, 5))
    billing.record_usage("s1", "e2", 15, D(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 3000),
        ("overage", "Overage: Metered", 50),
    ]
    assert invoice.total_cents == 3050


def test_event_at_period_end_is_not_billed_yet(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 100, D(2026, 1, 31))

    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]

    # Next period is [Jan 31, Mar 2): 30 days; the event is now due.
    second = billing.generate_invoice("s1")
    assert lines(second)[1] == ("overage", "Overage: Metered", 700)


def test_event_is_billed_only_once(billing, store):
    use_plan(store, "metered")
    billing.record_usage("s1", "e1", 40, D(2026, 1, 5))

    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3000


def test_late_event_is_billed_on_open_period(billing, store):
    use_plan(store, "metered")
    billing.generate_invoice("s1")  # closes January
    billing.record_usage("s1", "late", 40, D(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Metered", 100)


def test_included_units_are_floored_per_segment(billing, store):
    # 30 included over 30 days; a 10-day segment includes 10, a 20-day one 20.
    use_plan(store, "metered")
    billing.change_plan("s1", "pro", D(2026, 1, 11))  # metered 10d, pro 20d
    # pro: floor(60 * 20 / 30) = 40 included
    billing.record_usage("s1", "a", 12, D(2026, 1, 2))  # metered: 12 - 10 = 2 over
    billing.record_usage("s1", "b", 45, D(2026, 1, 20))  # pro: 45 - 40 = 5 over

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Metered", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Metered", 20),
        ("overage", "Overage: Pro", 100),
    ]


def test_included_units_floor_rounds_down(billing, store):
    use_plan(store, "metered")
    billing.change_plan("s1", "pro", D(2026, 1, 14))  # metered 13d: floor(13) = 13
    billing.record_usage("s1", "a", 14, D(2026, 1, 2))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Metered", 10) in lines(invoice)


def test_late_event_goes_to_first_segment(billing, store):
    use_plan(store, "metered")
    billing.generate_invoice("s1")
    billing.change_plan("s1", "pro", D(2026, 2, 15))  # period [Jan 31, Mar 2)
    billing.record_usage("s1", "late", 100, D(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    overage = [i for i in invoice.line_items if i.kind == "overage"]
    # first segment: Metered, 15 days, includes 15 -> 85 over * 10
    assert [(i.description, i.amount_cents) for i in overage] == [
        ("Overage: Metered", 850)
    ]


def test_event_on_effective_date_belongs_to_new_plan(billing, store):
    use_plan(store, "metered")
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.record_usage("s1", "a", 100, D(2026, 1, 11))
    billing.record_usage("s1", "b", 100, D(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    overage = {i.description: i.amount_cents for i in invoice.line_items if i.kind == "overage"}
    assert overage == {
        "Overage: Metered": 900,  # 100 - 10 included
        "Overage: Pro": 1200,  # 100 - 40 included
    }


# --- change_plan ------------------------------------------------------------


def test_mid_cycle_change_prorates_plan_lines(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_proration_uses_round_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    store.get_subscription("s1").plan_id = "odd"
    billing.change_plan("s1", "basic", D(2026, 1, 16))  # 15 of 30 days

    # 1005 * 15 / 30 = 502.5 -> 503
    assert lines(billing.generate_invoice("s1"))[0] == ("plan", "Plan: Odd", 503)


def test_multiple_changes_in_one_period(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.change_plan("s1", "metered", D(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Metered", 1000),
    ]


def test_change_back_to_an_earlier_plan_is_allowed(billing):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.change_plan("s1", "basic", D(2026, 1, 21))


def test_state_after_invoice_with_changes(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    billing.change_plan("s1", "metered", D(2026, 1, 21))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "metered"
    assert subscription.plan_changes == []
    assert subscription.period_start == D(2026, 1, 31)

    # next period is a single segment on the new plan
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Metered", 3000)]


@pytest.mark.parametrize(
    "effective_on",
    [D(2026, 1, 1), D(2025, 12, 1), D(2026, 1, 31), D(2026, 3, 1)],
)
def test_effective_date_must_be_strictly_inside_period(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_effective_date_must_be_after_previous_change(billing, store):
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    for day in (D(2026, 1, 11), D(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "metered", day)
    assert store.get_subscription("s1").plan_changes == [(D(2026, 1, 11), "pro")]


def test_changing_to_current_plan_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", D(2026, 1, 11))
    billing.change_plan("s1", "pro", D(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", D(2026, 1, 21))
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_unknown_plan_and_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", D(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", D(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


# --- combined with discount / credit / tax ----------------------------------


def test_discount_credit_tax_apply_to_plan_and_overage_subtotal(billing, store):
    use_plan(store, "metered")
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.record_usage("s1", "e1", 50, D(2026, 1, 5))  # 20 over -> 200

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
