from decimal import Decimal

from ledger.discounts import compute_discount_cents
from ledger.models import (
    Customer,
    DiscountCode,
    Invoice,
    LineItem,
    Plan,
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
    segments: list[tuple[Plan, int, int]] | None = None,
) -> Invoice:
    """Build an invoice from (plan, segment days, usage units) segments."""
    period_days = (subscription.period_end - subscription.period_start).days
    if segments is None:
        segments = [(plan, period_days, 0)]

    line_items: list[LineItem] = []
    for segment_plan, segment_days, _ in segments:
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents)
            * segment_days
            / period_days
        )
        line_items.append(
            LineItem("plan", f"Plan: {segment_plan.name}", amount)
        )

    for segment_plan, segment_days, segment_units in segments:
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units - included)
        if overage_units:
            line_items.append(
                LineItem(
                    "overage",
                    f"Overage: {segment_plan.name}",
                    overage_units * segment_plan.overage_unit_price_cents,
                )
            )

    subtotal_cents = sum(
        item.amount_cents for item in line_items if item.kind in ("plan", "overage")
    )
    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    # Credit is spent after the discount and before tax.
    amount_after_discount = subtotal_cents - discount_cents
    credit_cents = min(max(customer.credit_balance_cents, 0), amount_after_discount)
    if credit_cents:
        line_items.append(LineItem("credit", "Account credit", -credit_cents))

    taxable_cents = amount_after_discount - credit_cents
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
