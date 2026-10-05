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
    plans: dict[str, Plan] | None = None,
    plan_changes: list[PlanChange] | None = None,
    usage_events: list[UsageEvent] | None = None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    plans = plans or {plan.plan_id: plan}
    plan_changes = plan_changes or []
    usage_events = usage_events or []

    segments: list[tuple[Plan, int]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change in plan_changes:
        segments.append((segment_plan, (change.effective_on - segment_start).days))
        segment_start = change.effective_on
        segment_plan = plans[change.plan_id]
    segments.append((segment_plan, (subscription.period_end - segment_start).days))

    segment_units = [0 for _ in segments]
    boundaries = [subscription.period_start] + [
        change.effective_on for change in plan_changes
    ]
    for event in usage_events:
        segment_index = 0
        if event.occurred_on >= subscription.period_start:
            for index, boundary in enumerate(boundaries[1:], start=1):
                if event.occurred_on < boundary:
                    break
                segment_index = index
        segment_units[segment_index] += event.units

    line_items: list[LineItem] = []
    overage_items: list[LineItem] = []
    for index, (segment_plan, segment_days) in enumerate(segments):
        plan_amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(
            LineItem("plan", f"Plan: {segment_plan.name}", plan_amount)
        )
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units[index] - included)
        if overage_units:
            overage_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment_plan.name}",
                    overage_units * segment_plan.overage_unit_price_cents,
                )
            )

    line_items.extend(overage_items)
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
