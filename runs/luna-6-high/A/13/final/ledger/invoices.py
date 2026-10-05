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


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    *,
    segments: list[tuple[date, date, Plan]] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    if segments is None:
        segments = [(subscription.period_start, subscription.period_end, plan)]
    if usage_events is None:
        usage_events = []

    line_items: list[LineItem] = []
    segment_units = [0 for _ in segments]
    for event in usage_events:
        # Events before the open period are late events and belong to its first segment.
        segment_index = 0
        if event.occurred_on >= subscription.period_start:
            for index, (start, end, _) in enumerate(segments):
                if start <= event.occurred_on < end:
                    segment_index = index
                    break
        segment_units[segment_index] += event.units

    for (start, end, segment_plan), units in zip(segments, segment_units):
        segment_days = (end - start).days
        plan_amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * segment_days
            / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", plan_amount))

    # Overage lines follow all plan lines and remain in segment order.
    for (start, end, segment_plan), units in zip(segments, segment_units):
        segment_days = (end - start).days
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, units - included)
        if overage_units:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment_plan.name}",
                    overage_units * segment_plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(
        item.amount_cents for item in line_items if item.kind in {"plan", "overage"}
    )

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after the discount and before tax.
    credit_cents = min(
        customer.credit_balance_cents, max(0, subtotal_cents - discount_cents)
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
