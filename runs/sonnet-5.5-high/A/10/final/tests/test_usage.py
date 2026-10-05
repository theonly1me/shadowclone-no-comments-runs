from datetime import date
from decimal import Decimal

import pytest

from ledger.models import DiscountCode, Plan


@pytest.fixture(autouse=True)
def plans(store):
    store.add_plan(
        Plan("basic", "Basic", 3000, included_units=100, overage_unit_price_cents=10)
    )
    store.add_plan(
        Plan("pro", "Pro", 6000, included_units=300, overage_unit_price_cents=5)
    )


def lines(invoice):
    return [(i.kind, i.description, i.amount_cents) for i in invoice.line_items]


def test_record_usage_is_idempotent_per_subscription(billing):
    assert billing.record_usage("s1", "e1", 5, date(2026, 1, 2)) is True
    assert billing.record_usage("s1", "e1", 999, date(2026, 1, 3)) is False


def test_record_usage_validation(billing):
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", 0, date(2026, 1, 2))
    with pytest.raises(ValueError):
        billing.record_usage("s1", "e1", -1, date(2026, 1, 2))
    with pytest.raises(KeyError):
        billing.record_usage("nope", "e1", 1, date(2026, 1, 2))
    # the rejected calls did not consume the event id
    assert billing.record_usage("s1", "e1", 1, date(2026, 1, 2)) is True


def test_duplicate_event_does_not_change_billed_units(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 2))
    billing.record_usage("s1", "e1", 1000, date(2026, 1, 3))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1] == ("overage", "Overage: Basic", 500)


def test_duplicate_event_after_billing_is_still_ignored(billing):
    billing.record_usage("s1", "e1", 150, date(2026, 1, 2))
    billing.generate_invoice("s1")

    assert billing.record_usage("s1", "e1", 150, date(2026, 2, 2)) is False
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Basic", 3000)]


def test_usage_within_allowance_adds_no_overage(billing):
    billing.record_usage("s1", "e1", 100, date(2026, 1, 2))

    assert [i.kind for i in billing.generate_invoice("s1").line_items] == ["plan"]


def test_event_after_period_end_is_billed_later_and_once(billing):
    billing.record_usage("s1", "e1", 110, date(2026, 1, 31))
    billing.record_usage("s1", "e2", 110, date(2026, 1, 30))

    first = billing.generate_invoice("s1")
    second = billing.generate_invoice("s1")
    third = billing.generate_invoice("s1")

    assert lines(first)[1:] == [("overage", "Overage: Basic", 100)]
    assert lines(second)[1:] == [("overage", "Overage: Basic", 100)]
    assert lines(third) == [("plan", "Plan: Basic", 3000)]


def test_late_event_is_billed_on_open_period(billing):
    billing.generate_invoice("s1")
    billing.record_usage("s1", "late", 120, date(2025, 12, 15))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[1:] == [("overage", "Overage: Basic", 200)]


def test_change_plan_prorates_plan_lines(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    # 10 of 30 days on basic, 20 of 30 on pro
    assert lines(invoice) == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 4000),
    ]
    assert invoice.total_cents == 5000


def test_plan_line_rounds_half_up(billing, store):
    store.add_plan(Plan("odd", "Odd", 1005))
    billing.change_plan("s1", "odd", date(2026, 1, 16))

    invoice = billing.generate_invoice("s1")

    # 1005 * 15 / 30 = 502.5 -> 503
    assert lines(invoice)[1] == ("plan", "Plan: Odd", 503)


def test_plan_change_state_is_reset_after_invoice(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.generate_invoice("s1")

    subscription = store.get_subscription("s1")
    assert subscription.plan_id == "pro"
    assert subscription.plan_changes == []
    assert lines(billing.generate_invoice("s1")) == [("plan", "Plan: Pro", 6000)]


def test_usage_is_attributed_to_segment_by_date(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    # basic segment: 10 days -> included 100*10//30 = 33
    billing.record_usage("s1", "a", 40, date(2026, 1, 10))
    # pro segment: 20 days -> included 300*20//30 = 200
    billing.record_usage("s1", "b", 210, date(2026, 1, 11))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [
        ("overage", "Overage: Basic", 70),
        ("overage", "Overage: Pro", 50),
    ]


def test_late_event_goes_to_first_segment(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "late", 43, date(2025, 12, 1))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[2:] == [("overage", "Overage: Basic", 100)]


def test_multiple_changes_in_one_period(billing):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.change_plan("s1", "basic", date(2026, 1, 21))

    invoice = billing.generate_invoice("s1")

    assert lines(invoice)[:3] == [
        ("plan", "Plan: Basic", 1000),
        ("plan", "Plan: Pro", 2000),
        ("plan", "Plan: Basic", 1000),
    ]
    assert invoice.total_cents == 4000
    assert billing._store.get_subscription("s1").plan_id == "basic"


def test_change_plan_rejections_leave_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    subscription = store.get_subscription("s1")
    snapshot = list(subscription.plan_changes)

    for plan_id, day in [
        ("basic", date(2026, 1, 1)),  # == period_start
        ("basic", date(2026, 1, 31)),  # == period_end
        ("basic", date(2026, 2, 5)),  # after period
        ("basic", date(2026, 1, 11)),  # == previous change
        ("basic", date(2026, 1, 5)),  # before previous change
        ("pro", date(2026, 1, 20)),  # already current plan
    ]:
        with pytest.raises(ValueError):
            billing.change_plan("s1", plan_id, day)
    with pytest.raises(KeyError):
        billing.change_plan("s1", "ghost", date(2026, 1, 20))
    with pytest.raises(KeyError):
        billing.change_plan("nope", "pro", date(2026, 1, 20))

    assert subscription.plan_changes == snapshot
    assert subscription.plan_id == "basic"


def test_change_to_current_plan_without_changes_is_rejected(billing):
    with pytest.raises(ValueError):
        billing.change_plan("s1", "basic", date(2026, 1, 11))


def test_full_invoice_with_overage_discount_credit_and_tax(billing, store):
    store.add_discount_code(DiscountCode("TEN", percent_off=Decimal("10")))
    customer = store.get_customer("c1")
    customer.tax_rate_percent = Decimal("10")
    customer.credit_balance_cents = 500
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 43, date(2026, 1, 5))  # 10 over on basic

    invoice = billing.generate_invoice("s1", discount_code="TEN")

    # subtotal 1000 + 4000 + 100 = 5100
    assert [(i.kind, i.amount_cents) for i in invoice.line_items] == [
        ("plan", 1000),
        ("plan", 4000),
        ("overage", 100),
        ("discount", -510),
        ("credit", -500),
        ("tax", 409),  # 4090 * 10% = 409
    ]
    assert invoice.total_cents == 5100 - 510 - 500 + 409
    assert customer.credit_balance_cents == 0
    assert store.get_invoice(invoice.invoice_id) == invoice


def test_unknown_discount_code_leaves_state_unchanged(billing, store):
    billing.change_plan("s1", "pro", date(2026, 1, 11))
    billing.record_usage("s1", "a", 500, date(2026, 1, 5))

    with pytest.raises(KeyError):
        billing.generate_invoice("s1", discount_code="NOPE")

    subscription = store.get_subscription("s1")
    assert len(subscription.plan_changes) == 1
    assert subscription.period_start == date(2026, 1, 1)
    assert not any(e.billed for e in store.get_usage_events("s1"))
