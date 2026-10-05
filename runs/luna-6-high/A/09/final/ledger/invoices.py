from datetime import date
from decimal import Decimal
from typing import Callable

from ledger.discounts import compute_discount_cents
from ledger.models import Customer, DiscountCode, Invoice, LineItem, Plan, Subscription
from ledger.money import round_half_up_cents
from ledger.storage import PlanChange, UsageEvent
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    plan_changes: list[PlanChange] | None = None,
    usage_events: list[UsageEvent] | None = None,
    plan_lookup: Callable[[str], Plan] | None = None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    changes = plan_changes or []
    events = usage_events or []

    segments: list[tuple[date, date, Plan]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change in changes:
        segments.append(
            (segment_start, change.effective_on, segment_plan)
        )
        segment_start = change.effective_on
        if plan_lookup is None:
            raise ValueError("plan_lookup is required when plan changes are present")
        segment_plan = plan_lookup(change.plan_id)
    segments.append((segment_start, subscription.period_end, segment_plan))

    segment_units = [0 for _ in segments]
    for event in events:
        # Events before the current period are late and belong to its first segment.
        idx = 0
        if event.occurred_on >= subscription.period_start:
            for i, (start, end, _) in enumerate(segments):
                if start <= event.occurred_on < end:
                    idx = i
                    break
        segment_units[idx] += event.units

    line_items: list[LineItem] = []
    for (start, end, segment_plan), units in zip(segments, segment_units):
        segment_days = (end - start).days
        plan_amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents) * segment_days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", plan_amount))

    # Overage lines follow all plan lines, in chronological segment order.
    for (start, end, segment_plan), units in zip(segments, segment_units):
        segment_days = (end - start).days
        included = segment_plan.included_units * segment_days // period_days
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
