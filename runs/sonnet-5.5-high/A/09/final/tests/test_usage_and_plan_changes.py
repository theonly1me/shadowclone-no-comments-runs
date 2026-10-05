from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=30, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=60, overage_unit_price_cents=5)
    )


def kinds(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


# --- record_usage ---------------------------------------------------------


def test_record_usage_returns_true_then_false_for_duplicate(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 5)) is True
    assert billing.record_usage("s1", "e1", 99, date(2026, 1, 6)) is False


def test_duplicate_event_is_ignored_completely(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.record_usage("s1", "e1", 1000, date(2026, 2, 5))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
    ]


def test_event_ids_are_scoped_per_subscription(billing, store):
    store.add_subscription(
        Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31))
    )
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True
    assert billing.record_usage("s2", "e1", 1, date(2026, 1, 5)) is True


def test_event_id_stays_deduplicated_after_billing(billing):
    billing.record_usage("s1", "e1", 40, date(2026, 1, 5))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 40, date(2026, 1, 5)) is False


@pytest.mark.parametrize("units", [0, -3])
def test_non_positive_units_rejected(billing, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", units, date(2026, 1, 5))
    # nothing was recorded, so the id is still free
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 5)) is True


def test_record_usage_unknown_subscription(billing):
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 5))


# --- usage billing --------------------------------------------------------


def test_usage_within_included_units_has_no_overage(billing):
    billing.record_usage("s1", "e1", 30, date(2026, 1, 5))

    assert kinds(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_overage_line(billing):
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))
    billing.record_usage("s1", "e2", 25, date(2026, 1, 30))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 150),
    ]
    assert invoice.total_cents == 3150


def test_event_on_period_end_is_not_billed_yet(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 31))

    first = billing.generate_invoice("s1")
    assert [i.kind for i in first.line_items] == ["plan"]

    second = billing.generate_invoice("s1")
    assert ("overage", "Overage: Basic", 700) in kinds(second)


def test_event_is_billed_only_once(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 5))

    assert billing.generate_invoice("s1").total_cents == 3700
    assert billing.generate_invoice("s1").total_cents == 3000


def test_late_event_is_billed_on_the_open_period(billing):
    billing.generate_invoice("s1")  # period is now 2026-01-31 .. 2026-03-02
    billing.record_usage("s1", "late", 40, date(2026, 1, 10))

    invoice = billing.generate_invoice("s1")

    assert ("overage", "Overage: Basic", 100) in kinds(invoice)


def test_included_units_are_prorated_with_floor(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 8))  # basic 7d, pro 23d
    # basic included = 30*7//30 = 7, pro included = 60*23//30 = 46
    billing.record_usage("s1", "e1", 10, date(2026, 1, 2))
    billing.record_usage("s1", "e2", 50, date(2026, 1, 20))

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 700),
        ("plan", "Plan: Pro", 4600),
        ("overage", "Overage: Basic", 30),
        ("overage", "Overage: Pro", 20),
    ]


# --- change_plan ----------------------------------------------------------


def test_plan_change_splits_the_period(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # 10 days basic, 20 pro

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []


def test_next_period_bills_the_new_plan_in_full(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    assert kinds(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 6000)]


def test_multiple_changes_split_into_segments(billing, store):
    store.add_plan(Plan("lite", "Lite", 1000))
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # basic 10
    billing.change_plan("s1", "lite", date(2026, 1, 12))  # pro 1
    billing.change_plan("s1", "basic", date(2026, 1, 21))  # lite 9, basic 10

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 200),
        ("plan", "Plan: Lite", 300),
        ("plan", "Plan: Basic", 1000),
    ]
    assert store.get_subscription("s1").plan_id == "basic"


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    billing.change_plan("s1", "odd", date(2026, 1, 16))  # 15/30 days each

    invoice = billing.generate_invoice("s1")

    # 3000*15/30 = 1500, 1005*15/30 = 502.5 -> 503
    assert [i.amount_cents for i in invoice.line_items] == [1500, 503]


def test_late_event_is_attributed_to_first_segment(billing):
    billing.generate_invoice("s1")  # period 2026-01-31 .. 2026-03-02 (30 days)
    billing.change_plan("s1", "pro", date(2026, 2, 10))  # basic 10d, pro 20d
    billing.record_usage("s1", "late", 20, date(2026, 1, 1))

    invoice = billing.generate_invoice("s1")

    # basic included = 30*10//30 = 10 -> 10 overage * 10
    assert kinds(invoice)[2:] == [("overage", "Overage: Basic", 100)]


def test_event_on_effective_date_belongs_to_new_plan(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))  # pro included = 40
    billing.record_usage("s1", "e1", 41, date(2026, 1, 11))
    billing.record_usage("s1", "e2", 11, date(2026, 1, 10))  # basic included 10

    invoice = billing.generate_invoice("s1")

    assert kinds(invoice)[2:] == [
        ("overage", "Overage: Basic", 10),
        ("overage", "Overage: Pro", 5),
    ]


@pytest.mark.parametrize(
    "effective_on",
    [date(2026, 1, 1), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 5)],
)
def test_effective_date_must_be_inside_period(billing, store, effective_on):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1").plan_changes == []


def test_changes_must_be_strictly_increasing(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    for bad in (date(2026, 1, 11), date(2026, 1, 5)):
        with pytest.raises(ValueError):
            billing.change_plan("s1", "basic", bad)
    assert len(store.get_subscription("s1").plan_changes) == 1


def test_change_to_current_plan_rejected(billing, store):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", date(2026, 1, 15))
    assert len(store.get_subscription("s1").plan_changes) == 1
    # switching back is fine
    billing.change_plan("s1", "basic", date(2026, 1, 15))


def test_unknown_plan_and_subscription(billing, store):
    with pytest.raises(KeyError):
        billing.change_plan("s1", "nope", date(2026, 1, 11))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 11))
    assert store.get_subscription("s1").plan_changes == []


# --- discount / credit / tax on the new subtotal ---------------------------


def test_discount_credit_tax_apply_to_plan_and_overage(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "e1", 20, date(2026, 1, 5))  # basic incl 10 -> 100

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1000 + 4000 + 100 = 5100; discount 510; credit 500; tax 409
    assert kinds(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
        ("overage", "Overage: Basic", 100),
        ("discount", "Discount", -510),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 409),
    ]
    assert invoice.total_cents == 4499
    assert customer.credit_balance_cents == 0
