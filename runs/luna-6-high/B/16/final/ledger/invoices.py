from datetime import date
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
    available_plans = plans or {plan.plan_id: plan}
    changes = plan_changes or []
    segments: list[tuple[Plan, date, date, int]] = []
    segment_start = subscription.period_start
    current_plan = plan

    for change in changes:
        segment_days = (change.effective_on - segment_start).days
        segments.append((current_plan, segment_start, change.effective_on, segment_days))
        segment_start = change.effective_on
        current_plan = available_plans[change.plan_id]

    segment_days = (subscription.period_end - segment_start).days
    segments.append((current_plan, segment_start, subscription.period_end, segment_days))

    line_items: list[LineItem] = []
    for segment_plan, _, _, days in segments:
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents) * days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

    segment_units = [0 for _ in segments]
    for event in usage_events or []:
        segment_index = 0
        if event.occurred_on >= subscription.period_start:
            for index, (_, start, end, _) in enumerate(segments):
                if start <= event.occurred_on < end:
                    segment_index = index
                    break
        segment_units[segment_index] += event.units

    for index, (segment_plan, _, _, days) in enumerate(segments):
        included = segment_plan.included_units * days // period_days
        overage_units = max(0, segment_units[index] - included)
        if overage_units:
            amount = overage_units * segment_plan.overage_unit_price_cents
            line_items.append(LineItem("overage", f"Overage: {segment_plan.name}", amount))

    subtotal_cents = sum(item.amount_cents for item in line_items)
    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    amount_left = subtotal_cents - discount_cents
    credit_cents = min(customer.credit_balance_cents, amount_left)
    if credit_cents:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = amount_left - credit_cents
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
