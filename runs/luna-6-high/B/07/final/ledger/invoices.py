from datetime import date
from decimal import Decimal
from typing import Callable

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
    plan_changes: list[PlanChange] | None = None,
    usage_events: list[UsageEvent] | None = None,
    get_plan: Callable[[str], Plan] | None = None,
) -> Invoice:
    changes = plan_changes or []
    events = usage_events or []
    period_days = (subscription.period_end - subscription.period_start).days
    segments: list[tuple[Plan, date, date]] = []
    segment_start = subscription.period_start
    segment_plan = plan
    for change in changes:
        segments.append((segment_plan, segment_start, change.effective_on))
        segment_start = change.effective_on
        if get_plan is None:
            raise ValueError("plan lookup is required for plan changes")
        segment_plan = get_plan(change.plan_id)
    segments.append((segment_plan, segment_start, subscription.period_end))

    line_items = []
    segment_units = [0 for _ in segments]
    for event in events:
        if event.occurred_on < subscription.period_start:
            segment_units[0] += event.units
        else:
            for index, (_, start, end) in enumerate(segments):
                if start <= event.occurred_on < end:
                    segment_units[index] += event.units
                    break

    for segment_plan, segment_start, segment_end in segments:
        segment_days = (segment_end - segment_start).days
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(
            LineItem("plan", f"Plan: {segment_plan.name}", amount)
        )

    for index, (segment_plan, segment_start, segment_end) in enumerate(segments):
        segment_days = (segment_end - segment_start).days
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

    # Credit is spent after the discount and before tax.
    amount_after_discount = subtotal_cents - discount_cents
    credit_cents = min(customer.credit_balance_cents, amount_after_discount)
    if credit_cents > 0:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = amount_after_discount - max(credit_cents, 0)
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
