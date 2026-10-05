from datetime import date
from decimal import Decimal

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
    *,
    segments: list[tuple[date, date, Plan]] | None = None,
) -> Invoice:
    if segments is None:
        segments = [(subscription.period_start, subscription.period_end, plan)]
    period_days = (subscription.period_end - subscription.period_start).days
    line_items = []
    overage_items = []
    events = [
        event for event in subscription.usage_events.values()
        if not event.billed and event.occurred_on < subscription.period_end
    ]
    for index, (segment_start, segment_end, segment_plan) in enumerate(segments):
        segment_days = (segment_end - segment_start).days
        amount_cents = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents) * segment_days / period_days
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount_cents))
        segment_units = sum(
            event.units for event in events
            if event.occurred_on < segment_end
            and (index == 0 or event.occurred_on >= segment_start)
        )
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units - included)
        if overage_units > 0:
            overage_items.append(LineItem(
                "overage", f"Overage: {segment_plan.name}",
                overage_units * segment_plan.overage_unit_price_cents,
            ))
    line_items.extend(overage_items)
    subtotal_cents = sum(item.amount_cents for item in line_items)

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

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
        period_start=subscription.period_start,
        period_end=subscription.period_end,
        line_items=line_items,
        total_cents=sum(item.amount_cents for item in line_items),
    )
