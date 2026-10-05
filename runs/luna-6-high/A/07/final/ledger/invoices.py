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
    segments: list[tuple[Plan, int]] | None = None,
    segment_units: list[int] | None = None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    if segments is None:
        segments = [(plan, period_days)]
    if segment_units is None:
        segment_units = [0] * len(segments)

    line_items: list[LineItem] = []
    for segment_plan, segment_days in segments:
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * Decimal(segment_days)
            / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

    subtotal_cents = sum(item.amount_cents for item in line_items)
    for (segment_plan, segment_days), units in zip(segments, segment_units):
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, units - included)
        if overage_units:
            amount = overage_units * segment_plan.overage_unit_price_cents
            line_items.append(
                LineItem("overage", f"Overage: {segment_plan.name}", amount)
            )
            subtotal_cents += amount

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after the discount and before tax.
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
