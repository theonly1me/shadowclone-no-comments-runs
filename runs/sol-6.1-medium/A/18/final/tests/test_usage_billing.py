from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


def configure_plans(store):
    store.add_plan(Plan("basic", "Basic", 3001, included_units=31,
                        overage_unit_price_cents=10))
    store.add_plan(Plan("pro", "Pro", 6001, included_units=61,
                        overage_unit_price_cents=20))


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents)
            for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("basic", "Basic", 3000)
    assert plan.included_units == plan.overage_unit_price_cents == 0


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_reserve_event_id(billing, store, units):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "event", units, date(2026, 1, 2))
    assert store.get_subscription("s1").usage_events == {}
    assert billing.record_usage("s1", "event", 1, date(2026, 1, 2))


def test_unknown_subscription_and_plan(billing, store):
    with pytest.raises(KeyError):
        billing.record_usage("missing", "event", 1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.change_plan("missing", "basic", date(2026, 1, 2))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.change_plan("s1", "missing", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_usage_idempotency_is_per_subscription_and_survives_billing(billing, store):
    configure_plans(store)
    store.add_subscription(Subscription("s2", "c1", "basic",
                                        date(2026, 1, 1), date(2026, 1, 31)))
    assert billing.record_usage("s1", "event", 40, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "event", 0, date(2027, 1, 2)) is False
    assert billing.record_usage("s2", "event", 50, date(2026, 1, 2)) is True
    # Event state lives in the store, not in a particular service instance.
    another_service = BillingService(store)
    assert another_service.generate_invoice("s1").total_cents == 3091
    assert another_service.record_usage("s1", "event", -1, None) is False
    assert another_service.generate_invoice("s1").total_cents == 3001
    assert another_service.generate_invoice("s2").total_cents == 3191


def test_future_and_late_events_are_billed_once(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, overage_unit_price_cents=10))
    billing.record_usage("s1", "start", 1, date(2026, 1, 1))
    billing.record_usage("s1", "end", 2, date(2026, 1, 31))
    billing.record_usage("s1", "future", 4, date(2026, 3, 2))
    assert billing.generate_invoice("s1").total_cents == 3010
    billing.record_usage("s1", "late", 8, date(2025, 12, 1))
    assert billing.generate_invoice("s1").total_cents == 3100
    assert billing.generate_invoice("s1").total_cents == 3040
    assert billing.generate_invoice("s1").total_cents == 3000


def test_multiple_changes_rounding_allowances_and_usage_boundaries(billing, store):
    configure_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))
    billing.record_usage("s1", "late", 3, date(2025, 12, 31))
    billing.record_usage("s1", "first", 9, date(2026, 1, 10))
    billing.record_usage("s1", "second", 23, date(2026, 1, 11))
    billing.record_usage("s1", "third", 14, date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 60),
        ("overage", "Overage: Basic", 40),
    ]
    assert invoice.total_cents == 4120
    assert invoice.period_start == date(2026, 1, 1)
    assert invoice.period_end == date(2026, 1, 31)
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_changes == []
    assert subscription.plan_id == "basic"
    assert billing.generate_invoice("s1").total_cents == 3001


def test_half_up_proration_and_final_plan_carries_forward(billing, store):
    configure_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 16))
    invoice = billing.generate_invoice("s1")
    assert [item.amount_cents for item in invoice.line_items] == [1501, 3001]
    assert store.get_subscription("s1").plan_id == "pro"
    assert billing.generate_invoice("s1").total_cents == 6001


@pytest.mark.parametrize("effective_on", [date(2025, 12, 31), date(2026, 1, 1),
                                          date(2026, 1, 31), date(2026, 2, 1)])
def test_changes_must_be_inside_period(billing, store, effective_on):
    configure_plans(store)
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "pro", effective_on)
    assert store.get_subscription("s1") == before


@pytest.mark.parametrize("new_plan_id,effective_on", [
    ("basic", date(2026, 1, 10)),
    ("basic", date(2026, 1, 11)),
    ("pro", date(2026, 1, 12)),
])
def test_rejected_changes_preserve_existing_change(billing, store, new_plan_id, effective_on):
    configure_plans(store)
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", new_plan_id, effective_on)
    assert store.get_subscription("s1") == before


def test_same_initial_plan_rejected(billing, store):
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 2))
    assert store.get_subscription("s1") == before


def test_usage_discount_credit_tax_order(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, included_units=10,
                        overage_unit_price_cents=100))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 15, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "TEN")
    assert [(item.kind, item.amount_cents) for item in invoice.line_items] == [
        ("plan", 3000), ("overage", 500), ("discount", -350),
        ("credit", -500), ("tax", 265),
    ]
    assert invoice.total_cents == 2915
    assert customer.credit_balance_cents == 0


def test_full_discount_omits_credit_and_tax(billing, store):
    store.add_plan(Plan("basic", "Basic", 3000, overage_unit_price_cents=100))
    store.add_discount_code(DiscountCode("FREE", amount_off_cents=10000))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    billing.record_usage("s1", "event", 5, date(2026, 1, 2))
    invoice = billing.generate_invoice("s1", "FREE")
    assert [item.kind for item in invoice.line_items] == ["plan", "overage", "discount"]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 5000


def test_zero_priced_overage_still_has_line(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 2))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 0),
    ]


def test_failed_invoice_does_not_consume_usage_or_changes(billing, store):
    configure_plans(store)
    billing.record_usage("s1", "event", 100, date(2026, 1, 2))
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        billing.generate_invoice("s1", "missing")
    assert store.get_subscription("s1") == before
    assert billing.generate_invoice("s1").total_cents == 5901
