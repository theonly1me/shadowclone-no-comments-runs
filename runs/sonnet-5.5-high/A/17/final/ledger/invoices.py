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


def build_segments(
    subscription: Subscription, plans: dict[str, Plan]
) -> list[tuple[Plan, date, date]]:
    """Split the open period into (plan, start, end) segments, end exclusive."""
    segments = []
    start = subscription.period_start
    plan_id = subscription.plan_id
    for effective_on, new_plan_id in subscription.plan_changes:
        segments.append((plans[plan_id], start, effective_on))
        start, plan_id = effective_on, new_plan_id
    segments.append((plans[plan_id], start, subscription.period_end))
    return segments


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[tuple[Plan, date, date]],
    usage: list[UsageEvent],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days

    segment_units = [0] * len(segments)
    for event in usage:
        index = 0  # late events (before the period) go to the first segment
        for i, (_, start, _) in enumerate(segments):
            if event.occurred_on >= start:
                index = i
        segment_units[index] += event.units

    line_items = []
    overage_items = []
    for (plan, start, end), units in zip(segments, segment_units):
        segment_days = (end - start).days
        line_items.append(
            LineItem(
                "plan",
                f"Plan: {plan.name}",
                round_half_up_cents(
                    Decimal(plan.monthly_price_cents) * segment_days / period_days
                ),
            )
        )
        included = plan.included_units * segment_days // period_days
        overage_units = max(0, units - included)
        if overage_units > 0:
            overage_items.append(
                LineItem(
                    "overage",
                    f"Overage: {plan.name}",
                    overage_units * plan.overage_unit_price_cents,
                )
            )
    line_items.extend(overage_items)
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
