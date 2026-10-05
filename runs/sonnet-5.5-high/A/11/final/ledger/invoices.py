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
    plans: dict[str, Plan] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    """Build an invoice for the open period without mutating anything.

    `plan` is the plan the period starts on; `plans` must contain every plan
    named in `subscription.plan_changes`.
    """
    period_start = subscription.period_start
    period_end = subscription.period_end
    period_days = (period_end - period_start).days

    # Segments: (start, end, plan), split at each recorded plan change.
    segments: list[tuple[date, date, Plan]] = []
    seg_start, seg_plan = period_start, plan
    for effective_on, plan_id in subscription.plan_changes:
        segments.append((seg_start, effective_on, seg_plan))
        seg_start, seg_plan = effective_on, (plans or {})[plan_id]
    segments.append((seg_start, period_end, seg_plan))

    segment_units = [0] * len(segments)
    for event in usage_events or []:
        index = 0  # late events (before period_start) go to the first segment
        for i, (start, end, _) in enumerate(segments):
            if start <= event.occurred_on < end:
                index = i
                break
        segment_units[index] += event.units

    line_items = []
    for start, end, seg_plan in segments:
        days = (end - start).days
        amount = round_half_up_cents(
            Decimal(seg_plan.monthly_price_cents) * days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {seg_plan.name}", amount))

    for (start, end, seg_plan), units in zip(segments, segment_units):
        days = (end - start).days
        included = seg_plan.included_units * days // period_days
        overage_units = max(0, units - included)
        if overage_units > 0:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {seg_plan.name}",
                    overage_units * seg_plan.overage_unit_price_cents,
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
