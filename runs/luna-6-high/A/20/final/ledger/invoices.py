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
    plans: dict[str, Plan] | None = None,
    plan_changes: list[PlanChange] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    """Build an invoice for the subscription's currently open billing period."""
    plans = plans or {plan.plan_id: plan}
    changes = list(plan_changes if plan_changes is not None else subscription.plan_changes)
    events = usage_events if usage_events is not None else subscription.usage_events
    changes.sort(key=lambda change: change.effective_on)

    period_days = (subscription.period_end - subscription.period_start).days
    segments: list[tuple[Plan, int]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change in changes:
        segment_days = (change.effective_on - segment_start).days
        segments.append((segment_plan, segment_days))
        segment_start = change.effective_on
        segment_plan = plans[change.plan_id]
    segment_days = (subscription.period_end - segment_start).days
    segments.append((segment_plan, segment_days))

    # Attribute each eligible, not-yet-billed event to exactly one segment.
    segment_units = [0 for _ in segments]
    for event in events:
        if event.billed or event.occurred_on >= subscription.period_end:
            continue
        index = 0
        if event.occurred_on >= subscription.period_start:
            for i, change in enumerate(changes):
                if event.occurred_on >= change.effective_on:
                    index = i + 1
                else:
                    break
        segment_units[index] += event.units

    line_items: list[LineItem] = []
    for segment_plan, days in segments:
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(days)
            / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

    # All overage rows follow all plan rows, in segment order.
    for index, (segment_plan, days) in enumerate(segments):
        included = segment_plan.included_units * days // period_days
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

    after_discount = subtotal_cents - discount_cents
    credit_cents = min(customer.credit_balance_cents, after_discount)
    if credit_cents > 0:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = after_discount - max(credit_cents, 0)
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
