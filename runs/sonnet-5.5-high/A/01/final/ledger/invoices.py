from dataclasses import dataclass
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
from ledger.tax import compute_tax_cents


@dataclass(frozen=True)
class Segment:
    start: date
    end: date
    plan: Plan


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[Segment],
    customer: Customer,
    discount: DiscountCode | None,
    usage_events: list[UsageEvent] = (),
) -> Invoice:
    period_start = subscription.period_start
    period_end = subscription.period_end
    period_days = (period_end - period_start).days

    def days_of(segment: Segment) -> int:
        return (segment.end - segment.start).days

    plan_items = []
    for segment in segments:
        if len(segments) == 1 or period_days <= 0:
            amount = segment.plan.monthly_price_cents
        else:
            amount = round_half_up_cents(
                Decimal(segment.plan.monthly_price_cents) * days_of(segment) / period_days
            )
        plan_items.append(LineItem("plan", f"Plan: {segment.plan.name}", amount))

    # Late events (before the period) count in the first segment.
    segment_units = [0] * len(segments)
    for event in usage_events:
        index = 0
        for i, segment in enumerate(segments):
            if segment.start <= event.occurred_on:
                index = i
        segment_units[index] += event.units

    overage_items = []
    for segment, units in zip(segments, segment_units):
        plan = segment.plan
        included = plan.included_units * days_of(segment) // period_days if period_days > 0 else 0
        overage_units = max(0, units - included)
        if overage_units > 0:
            overage_items.append(
                LineItem(
                    "overage",
                    f"Overage: {plan.name}",
                    overage_units * plan.overage_unit_price_cents,
                )
            )

    line_items = plan_items + overage_items
    subtotal_cents = sum(item.amount_cents for item in line_items)

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after the discount and before tax.
    credit_cents = min(customer.credit_balance_cents, subtotal_cents - discount_cents)
    if credit_cents > 0:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = subtotal_cents - discount_cents - max(credit_cents, 0)
    tax_cents = compute_tax_cents(taxable_cents, customer.tax_rate_percent)
    if tax_cents:
        line_items.append(LineItem("tax", "Tax", tax_cents))

    return Invoice(
        invoice_id=invoice_id,
        subscription_id=subscription.subscription_id,
        period_start=period_start,
        period_end=period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )
