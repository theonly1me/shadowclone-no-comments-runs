from datetime import date
from decimal import Decimal

from ledger.discounts import compute_discount_cents
from ledger.models import (
    Customer,
    DiscountCode,
    Invoice,
    LineItem,
    Plan,
    Subscription,
    UsageEvent,
)
from ledger.money import round_half_up_cents
from ledger.storage import InMemoryStore
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    store: InMemoryStore,
    customer: Customer,
    discount: DiscountCode | None,
    usage_events: list[UsageEvent],
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    segments: list[tuple[date, date, Plan]] = []
    segment_start = subscription.period_start
    plan = store.get_plan(subscription.plan_id)
    for change in subscription.plan_changes:
        segments.append((segment_start, change.effective_on, plan))
        segment_start = change.effective_on
        plan = store.get_plan(change.plan_id)
    segments.append((segment_start, subscription.period_end, plan))

    line_items = []
    segment_units = [0 for _ in segments]
    for event in usage_events:
        if event.occurred_on < subscription.period_start:
            segment_units[0] += event.units
            continue
        for index, (segment_start, segment_end, _) in enumerate(segments):
            if segment_start <= event.occurred_on < segment_end:
                segment_units[index] += event.units
                break

    for segment_start, segment_end, segment_plan in segments:
        segment_days = (segment_end - segment_start).days
        amount_cents = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(
            LineItem("plan", f"Plan: {segment_plan.name}", amount_cents)
        )

    for index, (segment_start, segment_end, segment_plan) in enumerate(segments):
        segment_days = (segment_end - segment_start).days
        included_units = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units[index] - included_units)
        if overage_units:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment_plan.name}",
                    overage_units * segment_plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(item.amount_cents for item in line_items)

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after the discount and before tax.
    amount_after_discount = subtotal_cents - discount_cents
    credit_cents = min(customer.credit_balance_cents, amount_after_discount)
    if credit_cents > 0:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = amount_after_discount - max(credit_cents, 0)
    tax_cents = compute_tax_cents(taxable_cents, customer.tax_rate_percent)
    if tax_cents:
        line_items.append(LineItem("tax", "Tax", tax_cents))

    return Invoice(
        invoice_id=invoice_id,
        subscription_id=subscription.subscription_id,
        period_start=subscription.period_start,
        period_end=subscription.period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )
