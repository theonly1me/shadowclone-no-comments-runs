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
    start: date
    end: date
    plan: Plan

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[PlanSegment],
    usage_events: list[UsageEvent],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    plan_lines: list[LineItem] = []
    segment_units = [0] * len(segments)

    for event in usage_events:
        # Events before the current period are late and charged to its first plan.
        index = 0
        for candidate, segment in enumerate(segments):
            if segment.start <= event.occurred_on < segment.end:
                index = candidate
                break
        segment_units[index] += event.units

    for segment, units in zip(segments, segment_units):
        plan = segment.plan
        price = round_half_up_cents(
            Decimal(plan.monthly_price_cents) * Decimal(segment.days) / Decimal(period_days)
        )
        plan_lines.append(LineItem("plan", f"Plan: {plan.name}", price))

    overage_lines: list[LineItem] = []
    for segment, units in zip(segments, segment_units):
        plan = segment.plan
        included = plan.included_units * segment.days // period_days
        overage_units = max(0, units - included)
        if overage_units:
            overage_lines.append(
                LineItem(
                    "overage",
                    f"Overage: {plan.name}",
                    overage_units * plan.overage_unit_price_cents,
                )
            )

    line_items = plan_lines + overage_lines
    subtotal_cents = sum(item.amount_cents for item in line_items)

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after discount and before tax.
    amount_after_discount = subtotal_cents - discount_cents
    credit_cents = min(max(customer.credit_balance_cents, 0), amount_after_discount)
    if credit_cents:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = amount_after_discount - credit_cents
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
