from datetime import date
from decimal import Decimal
from typing import Iterable

from ledger.discounts import compute_discount_cents
from ledger.models import (
    Customer,
    DiscountCode,
    Invoice,
    LineItem,
    Plan,
    PlanChange,
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
    plan_changes: Iterable[tuple[PlanChange, Plan]] = (),
    usage_events: Iterable[UsageEvent] = (),
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    changes = sorted(plan_changes, key=lambda item: item[0].effective_on)
    segments: list[tuple[date, date, Plan]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change, next_plan in changes:
        segments.append((segment_start, change.effective_on, segment_plan))
        segment_start = change.effective_on
        segment_plan = next_plan
    segments.append((segment_start, subscription.period_end, segment_plan))

    events = list(usage_events)
    segment_units = [0 for _ in segments]
    for event in events:
        if event.occurred_on >= subscription.period_end:
            continue
        index = 0
        if event.occurred_on >= subscription.period_start:
            for candidate, (start, end, _) in enumerate(segments):
                if start <= event.occurred_on < end:
                    index = candidate
                    break
        segment_units[index] += event.units

    line_items: list[LineItem] = []
    overages: list[LineItem] = []
    for (start, end, segment_plan), units in zip(segments, segment_units):
        segment_days = (end - start).days
        plan_amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", plan_amount))
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, units - included)
        if overage_units:
            overages.append(
                LineItem(
                    "overage",
                    f"Overage: {segment_plan.name}",
                    overage_units * segment_plan.overage_unit_price_cents,
                )
            )
    line_items.extend(overages)
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
