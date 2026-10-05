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
) -> Invoice:
    return build_segmented_invoice(
        invoice_id,
        subscription,
        [(subscription.period_start, subscription.period_end, plan, 0)],
        customer,
        discount,
    )


def build_segmented_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[tuple[date, date, Plan, int]],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    """Build an invoice from (start, end, plan, units) period segments."""
    period_days = (subscription.period_end - subscription.period_start).days
    line_items: list[LineItem] = []

    for segment_start, segment_end, plan, _ in segments:
        segment_days = (segment_end - segment_start).days
        amount = round_half_up_cents(
            Decimal(plan.monthly_price_cents) * Decimal(segment_days) / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {plan.name}", amount))

    # Overage charges follow all plan charges, in chronological segment order.
    for segment_start, segment_end, plan, units in segments:
        segment_days = (segment_end - segment_start).days
        included = plan.included_units * segment_days // period_days
        overage_units = max(0, units - included)
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

    # Credit is spent after the discount and before tax.
    amount_left = subtotal_cents - discount_cents
    credit_cents = min(max(customer.credit_balance_cents, 0), amount_left)
    if credit_cents > 0:
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
