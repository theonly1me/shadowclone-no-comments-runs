from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

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


@dataclass
class _Segment:
    plan: Plan
    start: date
    end: date
    units: int = 0

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def _build_segments(
    subscription: Subscription,
    plan: Plan,
    plan_changes: Iterable[PlanChange],
    get_plan: Callable[[str], Plan],
) -> list[_Segment]:
    segments = [_Segment(plan, subscription.period_start, subscription.period_end)]
    for change in plan_changes:
        segments[-1].end = change.effective_on
        segments.append(
            _Segment(get_plan(change.plan_id), change.effective_on, subscription.period_end)
        )
    return segments


def _attribute_usage(segments: list[_Segment], usage_events: Iterable[UsageEvent]) -> None:
    for event in usage_events:
        target = segments[0]  # late events land in the first segment
        for segment in segments:
            if segment.start <= event.occurred_on < segment.end:
                target = segment
                break
        target.units += event.units


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    plan_changes: Iterable[PlanChange] = (),
    usage_events: Iterable[UsageEvent] = (),
    get_plan: Callable[[str], Plan] | None = None,
) -> Invoice:
    """Build the invoice for the subscription's open period.

    `plan` is the plan in effect at the start of the period; `plan_changes`
    switch it mid-period. `usage_events` must already be filtered to the events
    billed on this invoice.
    """
    period_days = (subscription.period_end - subscription.period_start).days
    segments = _build_segments(subscription, plan, plan_changes, get_plan)
    _attribute_usage(segments, usage_events)

    line_items = [
        LineItem(
            "plan",
            f"Plan: {segment.plan.name}",
            round_half_up_cents(
                Decimal(segment.plan.monthly_price_cents) * segment.days / period_days
            ),
        )
        for segment in segments
    ]
    for segment in segments:
        included = segment.plan.included_units * segment.days // period_days
        overage_units = max(0, segment.units - included)
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
