from decimal import Decimal

from ledger.discounts import compute_discount_cents
from ledger.models import Customer, DiscountCode, Invoice, LineItem, Plan, Subscription
from ledger.money import round_half_up_cents
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plans: list[tuple[Plan, int, int]],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    segment_data: list[tuple[Plan, int, int, int]] = []
    usage_by_segment = [0 for _ in plans]

    for plan, segment_start_day, segment_end_day in plans:
        segment_days = segment_end_day - segment_start_day
        segment_data.append((plan, segment_days, segment_start_day, segment_end_day))

    for event in subscription.usage_events:
        if event.event_id in subscription.billed_usage_event_ids:
            continue
        if event.occurred_on >= subscription.period_end:
            continue
        event_day = (event.occurred_on - subscription.period_start).days
        if event.occurred_on < subscription.period_start:
            segment_index = 0
        else:
            segment_index = next(
                index
                for index, (_, _, segment_start_day, segment_end_day) in enumerate(segment_data)
                if segment_start_day <= event_day < segment_end_day
            )
        usage_by_segment[segment_index] += event.units

    line_items: list[LineItem] = []
    for plan, segment_days, _, _ in segment_data:
        price = round_half_up_cents(
            Decimal(plan.monthly_price_cents) * segment_days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {plan.name}", price))

    for index, (plan, segment_days, _, _) in enumerate(segment_data):
        included = plan.included_units * segment_days // period_days
        overage_units = max(0, usage_by_segment[index] - included)
        if overage_units:
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

    credit_cents = min(
        max(customer.credit_balance_cents, 0), subtotal_cents - discount_cents
    )
    if credit_cents:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = subtotal_cents - discount_cents - credit_cents
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
