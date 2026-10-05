from decimal import Decimal

from ledger.discounts import compute_discount_cents
from ledger.models import (
    Customer,
    DiscountCode,
    Invoice,
    LineItem,
    Plan,
    Segment,
    Subscription,
)
from ledger.money import round_half_up_cents
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    segments: list[Segment] | None = None,
) -> Invoice:
    if segments is None:
        segments = [Segment(plan, subscription.period_start, subscription.period_end)]
    period_days = (subscription.period_end - subscription.period_start).days

    line_items = [
        LineItem(
            "plan",
            f"Plan: {segment.plan.name}",
            _prorated_price_cents(segment, period_days),
        )
        for segment in segments
    ]
    for segment in segments:
        overage_units = max(0, segment.units - _included_units(segment, period_days))
        if overage_units > 0:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment.plan.name}",
                    overage_units * segment.plan.overage_unit_price_cents,
                )
            )
    subtotal_cents = sum(item.amount_cents for item in line_items)

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


def _prorated_price_cents(segment: Segment, period_days: int) -> int:
    if segment.days == period_days:
        return segment.plan.monthly_price_cents
    return round_half_up_cents(
        Decimal(segment.plan.monthly_price_cents) * segment.days / period_days
    )


def _included_units(segment: Segment, period_days: int) -> int:
    if segment.days == period_days:
        return segment.plan.included_units
    return segment.plan.included_units * segment.days // period_days
