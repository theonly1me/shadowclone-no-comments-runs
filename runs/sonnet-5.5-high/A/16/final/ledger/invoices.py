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


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    segments: list[Segment] | None = None,
    usage: list[UsageEvent] | None = None,
) -> Invoice:
    period_start = subscription.period_start
    period_end = subscription.period_end
    period_days = (period_end - period_start).days
    if segments is None:
        segments = [Segment(plan, period_start, period_end)]
    usage = usage or []

    line_items = []
    for segment in segments:
        line_items.append(
            LineItem(
                "plan",
                f"Plan: {segment.plan.name}",
                _prorate(segment.plan.monthly_price_cents, segment.days, period_days),
            )
        )

    units = [0] * len(segments)
    for event in usage:
        units[_segment_index(segments, event.occurred_on)] += event.units
    for segment, segment_units in zip(segments, units):
        included = _included_units(segment, period_days)
        overage_units = max(0, segment_units - included)
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
        period_start=period_start,
        period_end=period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )


def _prorate(price_cents: int, segment_days: int, period_days: int) -> int:
    if segment_days == period_days:
        return price_cents
    return round_half_up_cents(Decimal(price_cents) * segment_days / period_days)


def _included_units(segment: Segment, period_days: int) -> int:
    if segment.days == period_days:
        return segment.plan.included_units
    return segment.plan.included_units * segment.days // period_days


def _segment_index(segments: list[Segment], occurred_on: date) -> int:
    # Late events (before the period) belong to the first segment.
    for index, segment in enumerate(segments):
        if occurred_on < segment.end:
            return index
    return len(segments) - 1
