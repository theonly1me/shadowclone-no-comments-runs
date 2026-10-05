from dataclasses import dataclass
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
    line_items = [LineItem("plan", f"Plan: {plan.name}", plan.monthly_price_cents)]
    return _finish_invoice(
        invoice_id, subscription, customer, discount, line_items, plan.monthly_price_cents
    )


@dataclass(frozen=True)
class InvoiceSegment:
    plan: Plan
    start: date
    end: date
    units: int


def build_usage_invoice(
    invoice_id: str,
    subscription: Subscription,
    segments: list[InvoiceSegment],
    customer: Customer,
    discount: DiscountCode | None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    plan_lines: list[LineItem] = []
    overage_lines: list[LineItem] = []
    for segment in segments:
        segment_days = (segment.end - segment.start).days
        amount = round_half_up_cents(
            Decimal(segment.plan.monthly_price_cents) * segment_days / period_days
        )
        plan_lines.append(LineItem("plan", f"Plan: {segment.plan.name}", amount))
        included = segment.plan.included_units * segment_days // period_days
        overage_units = max(0, segment.units - included)
        if overage_units:
            overage_lines.append(
                LineItem(
                    "overage",
                    f"Overage: {segment.plan.name}",
                    overage_units * segment.plan.overage_unit_price_cents,
                )
            )
    items = plan_lines + overage_lines
    subtotal = sum(item.amount_cents for item in items)
    return _finish_invoice(invoice_id, subscription, customer, discount, items, subtotal)


def _finish_invoice(
    invoice_id: str,
    subscription: Subscription,
    customer: Customer,
    discount: DiscountCode | None,
    line_items: list[LineItem],
    subtotal_cents: int,
) -> Invoice:

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
