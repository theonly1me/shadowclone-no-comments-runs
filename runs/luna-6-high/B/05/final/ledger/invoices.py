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
    plan_lookup = plans or {plan.plan_id: plan}
    changes = plan_changes or []
    events = usage_events or []
    period_days = (subscription.period_end - subscription.period_start).days
    segments: list[tuple[Plan, int]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change in changes:
        next_plan = plan_lookup[change.plan_id]
        segments.append((segment_plan, (change.effective_on - segment_start).days))
        segment_start = change.effective_on
        segment_plan = next_plan
    segments.append((segment_plan, (subscription.period_end - segment_start).days))

    line_items: list[LineItem] = []
    segment_units = [0 for _ in segments]
    for event in events:
        if event.occurred_on >= subscription.period_end:
            continue
        index = 0
        if event.occurred_on >= subscription.period_start:
            for change_index, change in enumerate(changes):
                if event.occurred_on >= change.effective_on:
                    index = change_index + 1
                else:
                    break
        segment_units[index] += event.units

    for segment_plan, days in segments:
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents) * days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

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

    # Credit is spent after the discount and before tax.
    credit_cents = min(customer.credit_balance_cents, max(0, subtotal_cents - discount_cents))
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
