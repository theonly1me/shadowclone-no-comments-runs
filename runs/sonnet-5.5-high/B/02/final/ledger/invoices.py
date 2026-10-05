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
    plan_changes: list[tuple[date, Plan]] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    period_start = subscription.period_start
    period_end = subscription.period_end
    period_days = (period_end - period_start).days

    starts = [period_start] + [effective_on for effective_on, _ in plan_changes or []]
    ends = starts[1:] + [period_end]
    plans = [plan] + [changed_plan for _, changed_plan in plan_changes or []]
    segment_days = [(end - start).days for start, end in zip(starts, ends)]

    segment_units = [0] * len(plans)
    for event in usage_events or []:
        index = 0
        for i, start in enumerate(starts):
            if event.occurred_on >= start:
                index = i
        segment_units[index] += event.units

    line_items = []
    for segment_plan, days in zip(plans, segment_days):
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents) * days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

    for segment_plan, days, units in zip(plans, segment_days, segment_units):
        included = segment_plan.included_units * days // period_days
        overage_units = max(0, units - included)
        if overage_units > 0:
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
