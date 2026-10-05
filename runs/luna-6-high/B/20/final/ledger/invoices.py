from datetime import date
from decimal import Decimal

from ledger.discounts import compute_discount_cents
from ledger.models import Customer, DiscountCode, Invoice, LineItem, Plan, Subscription, UsageEvent
from ledger.money import round_half_up_cents
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[tuple[Plan, date, int]],
    usage_events: list[UsageEvent],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    segment_units = [0 for _ in segments]
    for event in usage_events:
        segment_index = 0
        if event.occurred_on >= subscription.period_start:
            for index, (_, segment_start, _) in enumerate(segments):
                if segment_start <= event.occurred_on:
                    segment_index = index
                else:
                    break
        segment_units[segment_index] += event.units

    line_items: list[LineItem] = []
    for plan, _, segment_days in segments:
        amount = round_half_up_cents(
            Decimal(plan.monthly_price_cents) * segment_days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {plan.name}", amount))
    for index, (plan, _, segment_days) in enumerate(segments):
        included = plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units[index] - included)
        if overage_units > 0:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {plan.name}",
                    overage_units * plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(item.amount_cents for item in line_items)
    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    after_discount = subtotal_cents - discount_cents
    credit_cents = min(customer.credit_balance_cents, max(0, after_discount))
    if credit_cents:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    tax_cents = compute_tax_cents(
        after_discount - credit_cents, customer.tax_rate_percent
    )
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
