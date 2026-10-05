from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def items(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


def test_usage_is_idempotent_even_with_invalid_replacement(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    assert billing.record_usage("s1", "event", 3, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2026, 5, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3030
    assert billing.record_usage("s1", "event", -1, date(2026, 1, 2)) is False
    assert billing.generate_invoice("s1").total_cents == 3000


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_leaves_state_unchanged(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2)) is True


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_events_are_scoped_to_subscription_and_survive_service_recreation(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "same", 1, date(2026, 1, 2))
    assert billing.record_usage("s2", "same", 2, date(2026, 1, 2))
    another = BillingService(store)
    assert another.generate_invoice("s1").total_cents == 3010
    assert another.generate_invoice("s2").total_cents == 3020
    assert not another.record_usage("s1", "same", 100, date(2026, 1, 2))


def test_late_and_future_events_are_billed_once(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    billing.record_usage("s1", "late", 2, date(2025, 12, 1))
    billing.record_usage("s1", "start", 3, date(2026, 1, 1))
    billing.record_usage("s1", "end", 4, date(2026, 1, 31))
    billing.record_usage("s1", "future", 5, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3050
    billing.record_usage("s1", "new-late", 6, date(2026, 1, 15))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3050
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_segments_usage_allowances_and_adjustment_order(billing, store):
    basic = store.get_plan("basic")
    basic.included_units = 10
    basic.overage_unit_price_cents = 20
    store.add_plan(Plan("pro", "Pro", 6000, included_units=20, overage_unit_price_cents=10))
    store.add_plan(Plan("premium", "Premium", 9000, included_units=30, overage_unit_price_cents=5))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "premium", date(2026, 1, 21))
    for event_id, units, occurred_on in [
        ("late", 2, date(2025, 12, 31)),
        ("first", 3, date(2026, 1, 10)),
        ("second", 9, date(2026, 1, 11)),
        ("third", 12, date(2026, 1, 21)),
    ]:
        billing.record_usage("s1", event_id, units, occurred_on)
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")

    invoice = BillingService(store).generate_invoice("s1", "TEN")

    assert items(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Premium", 3000),
        ("overage", "Overage: Basic", 40),
        ("overage", "Overage: Pro", 30),
        ("overage", "Overage: Premium", 10),
        ("discount", "Discount", -608),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 497),
    ]
    assert invoice.total_cents == 5469
    assert store.get_invoice(invoice.invoice_id) == invoice
    assert customer.credit_balance_cents == 0
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "premium"
    assert subscription.plan_changes == []
    assert subscription.period_start == date(2026, 1, 31)
    assert subscription.period_end == date(2026, 3, 2)
    customer.tax_rate_percent = Decimal("0")
    assert items(billing.generate_invoice("s1")) == [("plan", "Plan: Premium", 9000)]


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)])
def test_plan_change_must_be_strictly_inside_period(billing, store, effective_on):
    store.add_plan(Plan("pro", "Pro", 6000))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


def test_rejected_changes_preserve_history_and_current_plan(billing, store):
    store.add_plan(Plan("pro", "Pro", 6000))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    for plan_id, effective_on in [
        ("pro", date(2026, 1, 20)),
        ("basic", date(2026, 1, 16)),
        ("basic", date(2026, 1, 15)),
    ]:
        before = deepcopy(store.get_subscription("s1"))
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, effective_on)
        assert store.get_subscription("s1") == before
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 1000),
        ("plan", "Plan: Basic", 1000),
    ]
    billing.change_plan("s1", "pro", date(2026, 2, 1))


def test_segment_prices_round_half_up_independently(billing, store):
    store.get_plan("basic").monthly_price_cents = 1
    store.add_plan(Plan("pro", "Pro", 3))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1),
        ("plan", "Plan: Pro", 2),
    ]


def test_allowances_are_not_shared_between_segments(billing, store):
    store.get_plan("basic").included_units = 100
    store.add_plan(Plan("pro", "Pro", 6000, included_units=2, overage_unit_price_cents=10))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    billing.record_usage("s1", "event", 3, date(2026, 1, 16))
    assert items(billing.generate_invoice("s1"))[-1] == ("overage", "Overage: Pro", 20)


def test_zero_price_overage_still_has_a_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 2))
    assert items(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    store.get_plan("basic").overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, overage_unit_price_cents=20))
    billing.record_usage("s1", "event", 2, date(2026, 1, 16))
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 4540
