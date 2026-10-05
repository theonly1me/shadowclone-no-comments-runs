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
class PlanSegment:
    plan: Plan
    start: date
    end: date


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    segments: list[PlanSegment] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    if segments is None:
        segments = [PlanSegment(plan, subscription.period_start, subscription.period_end)]
    usage_events = usage_events or []

    period_days = (subscription.period_end - subscription.period_start).days
    segment_units = [0 for _ in segments]
    for event in usage_events:
        index = 0
        if event.occurred_on >= subscription.period_start:
            for candidate, segment in enumerate(segments):
                if segment.start <= event.occurred_on < segment.end:
                    index = candidate
                    break
        segment_units[index] += event.units

    line_items: list[LineItem] = []
    for segment in segments:
        segment_days = (segment.end - segment.start).days
        prorated_price = round_half_up_cents(
            Decimal(segment.plan.monthly_price_cents) * segment_days / period_days
        )
        line_items.append(
            LineItem("plan", f"Plan: {segment.plan.name}", prorated_price)
        )

    for segment, units in zip(segments, segment_units):
        segment_days = (segment.end - segment.start).days
        included = segment.plan.included_units * segment_days // period_days
        overage_units = max(0, units - included)
        if overage_units:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment.plan.name}",
                    overage_units * segment.plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(
        item.amount_cents for item in line_items if item.kind in {"plan", "overage"}
    )

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
        period_start=subscription.period_start,
        period_end=subscription.period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )
