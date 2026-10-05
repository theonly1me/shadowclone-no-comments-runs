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
class BillingSegment:
    start: date
    end: date
    plan: Plan


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[BillingSegment],
    usage_events: list[UsageEvent],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    plan_lines: list[LineItem] = []
    overage_lines: list[LineItem] = []

    for index, segment in enumerate(segments):
        segment_days = (segment.end - segment.start).days
        amount = round_half_up_cents(
            Decimal(segment.plan.monthly_price_cents) * segment_days / period_days
        )
        plan_lines.append(LineItem("plan", f"Plan: {segment.plan.name}", amount))

        segment_units = sum(
            event.units
            for event in usage_events
            if (event.occurred_on < subscription.period_start and index == 0)
            or (segment.start <= event.occurred_on < segment.end)
        )
        included = segment.plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units - included)
        if overage_units:
            overage_lines.append(
                LineItem(
                    "overage",
                    f"Overage: {segment.plan.name}",
                    overage_units * segment.plan.overage_unit_price_cents,
                )
            )

    line_items = plan_lines + overage_lines
    subtotal_cents = sum(item.amount_cents for item in line_items)

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after the discount and before tax.
    credit_cents = min(
        max(customer.credit_balance_cents, 0), subtotal_cents - discount_cents
    )
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
