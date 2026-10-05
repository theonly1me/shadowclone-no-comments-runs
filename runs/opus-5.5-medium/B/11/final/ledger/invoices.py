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
    plan: Plan
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def segment_index(segments: list[Segment], occurred_on: date) -> int:
    for index in range(len(segments) - 1, -1, -1):
        if occurred_on >= segments[index].start:
            return index
    return 0


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[Segment],
    usage_events: list[UsageEvent],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days

    segment_units = [0] * len(segments)
    for event in usage_events:
        segment_units[segment_index(segments, event.occurred_on)] += event.units

    line_items = []
    for segment in segments:
        amount = round_half_up_cents(
            Decimal(segment.plan.monthly_price_cents) * segment.days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment.plan.name}", amount))

    for segment, units in zip(segments, segment_units):
        included = segment.plan.included_units * segment.days // period_days
        overage_units = max(0, units - included)
        if overage_units > 0:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment.plan.name}",
                    overage_units * segment.plan.overage_unit_price_cents,
                )
            )

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
        period_start=subscription.period_start,
        period_end=subscription.period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )
