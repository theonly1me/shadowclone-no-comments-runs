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
    plans: list[Plan],
    customer: Customer,
    discount: DiscountCode | None,
    usage_events: list[UsageEvent],
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    boundaries = [subscription.period_start]
    boundaries.extend(change.effective_on for change in subscription.plan_changes)
    boundaries.append(subscription.period_end)
    line_items: list[LineItem] = []
    segment_units = [0 for _ in plans]

    for event in usage_events:
        if event.occurred_on < subscription.period_start:
            segment_units[0] += event.units
            continue
        for index, (segment_start, segment_end) in enumerate(
            zip(boundaries, boundaries[1:])
        ):
            if segment_start <= event.occurred_on < segment_end:
                segment_units[index] += event.units
                break

    for plan, segment_start, segment_end in zip(plans, boundaries, boundaries[1:]):
        segment_days = (segment_end - segment_start).days
        amount = round_half_up_cents(
            Decimal(plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {plan.name}", amount))

    for index, plan in enumerate(plans):
        segment_days = (boundaries[index + 1] - boundaries[index]).days
        included = plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units[index] - included)
        if overage_units > 0:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {plan.name}",
                    overage_units * plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(item.amount_cents for item in line_items)
    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

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
