from decimal import Decimal

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
    *,
    plan_changes: list[tuple[PlanChange, Plan]] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    """Build an invoice from the plan and usage records for this period."""
    period_days = (subscription.period_end - subscription.period_start).days
    changes = plan_changes or []
    events = usage_events or []

    segments: list[tuple[Plan, int]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change, next_plan in changes:
        segment_days = (change.effective_on - segment_start).days
        segments.append((segment_plan, segment_days))
        segment_start = change.effective_on
        segment_plan = next_plan
    segments.append(
        (segment_plan, (subscription.period_end - segment_start).days)
    )

    segment_units = [0 for _ in segments]
    for event in events:
        if event.billed or event.occurred_on >= subscription.period_end:
            continue
        if event.occurred_on < subscription.period_start:
            segment_index = 0
        else:
            segment_index = 0
            for index, (change, _) in enumerate(changes):
                if event.occurred_on >= change.effective_on:
                    segment_index = index + 1
                else:
                    break
        segment_units[segment_index] += event.units

    line_items: list[LineItem] = []
    for segment_plan, segment_days in segments:
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

    for index, (segment_plan, segment_days) in enumerate(segments):
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units[index] - included)
        if overage_units:
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

    # Credit is spent after discount and before tax.
    remaining_cents = max(0, subtotal_cents - discount_cents)
    credit_cents = min(max(0, customer.credit_balance_cents), remaining_cents)
    if credit_cents:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = remaining_cents - credit_cents
    tax_cents = compute_tax_cents(taxable_cents, customer.tax_rate_percent)
    if tax_cents:
        line_items.append(LineItem("tax", "Tax", tax_cents))

    invoice = Invoice(
        invoice_id=invoice_id,
        subscription_id=subscription.subscription_id,
        period_start=subscription.period_start,
        period_end=subscription.period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )
    return invoice
