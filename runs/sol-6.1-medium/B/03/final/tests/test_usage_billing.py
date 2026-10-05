from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from ledger.billing import BillingService
from ledger.models import DiscountCode, Plan, Subscription


@pytest.fixture
def metered(store):
    plan = store.get_plan("basic")
    plan.included_units = 5
    plan.overage_unit_price_cents = 10
    store.add_plan(Plan("pro", "Pro", 6000, included_units=7, overage_unit_price_cents=20))
    return BillingService(store)


def lines(invoice):
    return [(item.kind, item.description, item.amount_cents) for item in invoice.line_items]


def test_plan_usage_defaults():
    plan = Plan("free", "Free", 0)
    assert plan.included_units == 0
    assert plan.overage_unit_price_cents == 0


@pytest.mark.parametrize("units", [0, -1])
def test_invalid_usage_does_not_record_event(metered, store, units):
    with pytest.raises(ValueError):
        metered.record_usage("s1", "event", units, date(2026, 1, 5))
    assert store.get_subscription("s1").usage_events == {}
    assert metered.record_usage("s1", "event", 1, date(2026, 1, 5))


def test_unknown_subscription(metered):
    with pytest.raises(KeyError):
        metered.record_usage("missing", "event", 1, date(2026, 1, 5))
    with pytest.raises(KeyError):
        metered.change_plan("missing", "pro", date(2026, 1, 5))


def test_usage_idempotency_survives_invoices_and_service_instances(metered, store):
    assert metered.record_usage("s1", "event", 8, date(2026, 1, 5)) is True
    assert metered.record_usage("s1", "event", 0, date(2030, 1, 1)) is False
    invoice = BillingService(store).generate_invoice("s1")
    assert lines(invoice) == [("plan", "Plan: Basic", 3000), ("overage", "Overage: Basic", 30)]
    assert metered.record_usage("s1", "event", -1, date(2026, 1, 5)) is False
    assert metered.generate_invoice("s1").total_cents == 3000


def test_event_ids_are_scoped_to_subscription(metered, store):
    store.add_subscription(Subscription("s2", "c1", "basic", date(2026, 1, 1), date(2026, 1, 31)))
    assert metered.record_usage("s1", "event", 6, date(2026, 1, 5))
    assert metered.record_usage("s2", "event", 7, date(2026, 1, 5))
    assert metered.generate_invoice("s1").total_cents == 3010
    assert metered.generate_invoice("s2").total_cents == 3020


def test_future_events_wait_for_first_eligible_invoice(metered):
    metered.record_usage("s1", "boundary", 6, date(2026, 1, 31))
    metered.record_usage("s1", "future", 7, date(2026, 3, 2))
    assert metered.generate_invoice("s1").total_cents == 3000
    assert metered.generate_invoice("s1").total_cents == 3010
    assert metered.generate_invoice("s1").total_cents == 3020
    assert metered.generate_invoice("s1").total_cents == 3000


def test_late_event_billed_in_current_period_first_segment(metered):
    metered.generate_invoice("s1")
    metered.change_plan("s1", "pro", date(2026, 2, 15))
    metered.record_usage("s1", "late", 5, date(2025, 12, 1))
    invoice = metered.generate_invoice("s1")
    assert (invoice.period_start, invoice.period_end) == (date(2026, 1, 31), date(2026, 3, 2))
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1500),
        ("plan", "Plan: Pro", 3000),
        ("overage", "Overage: Basic", 30),
    ]
    assert metered.generate_invoice("s1").total_cents == 6000


def test_multiple_changes_boundaries_and_floored_allowances(metered, store):
    metered.change_plan("s1", "pro", date(2026, 1, 11))
    metered.change_plan("s1", "basic", date(2026, 1, 21))
    metered.record_usage("s1", "start", 3, date(2026, 1, 1))
    metered.record_usage("s1", "first-change", 3, date(2026, 1, 11))
    metered.record_usage("s1", "second-change", 2, date(2026, 1, 21))
    invoice = metered.generate_invoice("s1")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
        ("overage", "Overage: Basic", 20),
        ("overage", "Overage: Pro", 20),
        ("overage", "Overage: Basic", 10),
    ]
    assert invoice.total_cents == 4050
    assert store.get_invoice(invoice.invoice_id) == invoice
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "basic"
    assert subscription.plan_changes == []


def test_proration_rounds_each_segment_half_up(metered, store):
    store.get_plan("basic").monthly_price_cents = 3001
    store.get_plan("pro").monthly_price_cents = 6001
    metered.change_plan("s1", "pro", date(2026, 1, 16))
    assert lines(metered.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 1501),
        ("plan", "Plan: Pro", 3001),
    ]
    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    metered.change_plan("s1", "basic", date(2026, 2, 1))


@pytest.mark.parametrize(
    "plan_id, effective_on, error",
    [
        ("pro", date(2026, 1, 1), ValueError),
        ("pro", date(2025, 12, 31), ValueError),
        ("pro", date(2026, 1, 31), ValueError),
        ("pro", date(2026, 2, 1), ValueError),
        ("basic", date(2026, 1, 10), ValueError),
        ("unknown", date(2026, 1, 10), KeyError),
    ],
)
def test_rejected_first_change_preserves_state(metered, store, plan_id, effective_on, error):
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(error):
        metered.change_plan("s1", plan_id, effective_on)
    assert subscription == before


@pytest.mark.parametrize(
    "plan_id, effective_on",
    [
        ("basic", date(2026, 1, 9)),
        ("basic", date(2026, 1, 10)),
        ("pro", date(2026, 1, 11)),
    ],
)
def test_rejected_subsequent_change_preserves_state(metered, store, plan_id, effective_on):
    metered.change_plan("s1", "pro", date(2026, 1, 10))
    subscription = store.get_subscription("s1")
    before = deepcopy(subscription)
    with pytest.raises(ValueError):
        metered.change_plan("s1", plan_id, effective_on)
    assert subscription == before


def test_overage_precedes_discount_credit_and_tax(metered, store):
    metered.record_usage("s1", "event", 15, date(2026, 1, 5))
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 500
    customer.tax_rate_percent = Decimal("10")
    invoice = metered.generate_invoice("s1", "TEN")
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 100),
        ("discount", "Discount", -310),
        ("credit", "Account credit", -500),
        ("tax", "Tax", 229),
    ]
    assert invoice.total_cents == 2519
    assert customer.credit_balance_cents == 0


@pytest.mark.parametrize("units", [4, 5])
def test_usage_within_allowance_has_no_overage(metered, units):
    metered.record_usage("s1", "event", units, date(2026, 1, 5))
    assert lines(metered.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_zero_price_overage_line_is_still_added(billing):
    billing.record_usage("s1", "event", 1, date(2026, 1, 5))
    assert lines(billing.generate_invoice("s1")) == [
        ("plan", "Plan: Basic", 3000),
        ("overage", "Overage: Basic", 0),
    ]


def test_credit_and_discount_are_capped_including_overage(metered, store):
    metered.record_usage("s1", "event", 15, date(2026, 1, 5))
    store.add_discount_code(DiscountCode("FIXED", amount_off_cents=100))
    customer = store.get_customer("c1")
    customer.credit_balance_cents = 5000
    customer.tax_rate_percent = Decimal("10")
    invoice = metered.generate_invoice("s1", "FIXED")
    assert [item.amount_cents for item in invoice.line_items] == [3000, 100, -100, -3000]
    assert invoice.total_cents == 0
    assert customer.credit_balance_cents == 2000


def test_failed_invoice_does_not_consume_usage_or_changes(metered, store):
    metered.record_usage("s1", "event", 8, date(2026, 1, 5))
    metered.change_plan("s1", "pro", date(2026, 1, 16))
    before = deepcopy(store.get_subscription("s1"))
    with pytest.raises(KeyError):
        metered.generate_invoice("s1", "unknown")
    assert store.get_subscription("s1") == before
    assert metered.generate_invoice("s1").total_cents == 4560
