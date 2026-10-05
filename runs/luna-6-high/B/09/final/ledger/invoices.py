from datetime import date
from decimal import Decimal
from typing import Mapping

from ledger.discounts import compute_discount_cents
from ledger.models import Customer, DiscountCode, Invoice, LineItem, Plan, Subscription
from ledger.money import round_half_up_cents
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    plans: Mapping[str, Plan] | None = None,
) -> Invoice:
    available_plans = plans or {plan.plan_id: plan}
    period_days = (subscription.period_end - subscription.period_start).days
    segments: list[tuple[Plan, date, date, int]] = []
    segment_start = subscription.period_start
    segment_plan = plan

    for change in subscription.plan_changes:
        segment_days = (change.effective_on - segment_start).days
        segments.append((segment_plan, segment_start, change.effective_on, segment_days))
        segment_start = change.effective_on
        segment_plan = available_plans[change.plan_id]

    segment_days = (subscription.period_end - segment_start).days
    segments.append((segment_plan, segment_start, subscription.period_end, segment_days))

    segment_units = [0 for _ in segments]
    for event in subscription.usage_events:
        if event.billed or event.occurred_on >= subscription.period_end:
            continue
        segment_index = 0
        if event.occurred_on >= subscription.period_start:
            for index, (_, start, end, _) in enumerate(segments):
                if start <= event.occurred_on < end:
                    segment_index = index
                    break
        segment_units[segment_index] += event.units

    line_items = []
    for current_plan, _, _, days in segments:
        amount = round_half_up_cents(
            Decimal(current_plan.monthly_price_cents) * days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {current_plan.name}", amount))

    for index, (current_plan, _, _, days) in enumerate(segments):
        included = current_plan.included_units * days // period_days
        overage_units = max(0, segment_units[index] - included)
        if overage_units > 0:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {current_plan.name}",
                    overage_units * current_plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(item.amount_cents for item in line_items)
    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

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
