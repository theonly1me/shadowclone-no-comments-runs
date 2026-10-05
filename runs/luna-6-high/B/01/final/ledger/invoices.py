from datetime import date
from decimal import Decimal

from ledger.discounts import compute_discount_cents
from ledger.models import Customer, DiscountCode, Invoice, LineItem, Plan, PlanChange, Subscription, UsageEvent
from ledger.money import round_half_up_cents
from ledger.tax import compute_tax_cents


def build_invoice(
    invoice_id: str,
    subscription: Subscription,
    plan: Plan,
    customer: Customer,
    discount: DiscountCode | None,
    changes: list[PlanChange] | None = None,
    usage_events: list[UsageEvent] | None = None,
    plans: dict[str, Plan] | None = None,
) -> Invoice:
    period_days = (subscription.period_end - subscription.period_start).days
    segments: list[tuple[Plan, date, date]] = []
    segment_start = subscription.period_start
    current_plan = plan
    for change in changes or []:
        segments.append((current_plan, segment_start, change.effective_on))
        current_plan = (plans or {}).get(change.plan_id, plan if change.plan_id == plan.plan_id else None)
        if current_plan is None:
            raise KeyError(change.plan_id)
        segment_start = change.effective_on
    segments.append((current_plan, segment_start, subscription.period_end))

    line_items = []
    segment_units = [0 for _ in segments]
    billable_events = [event for event in usage_events or [] if not event.billed and event.occurred_on < subscription.period_end]
    for event in billable_events:
        event_index = 0
        if event.occurred_on >= subscription.period_start:
            for index, (_, start, end) in enumerate(segments):
                if start <= event.occurred_on < end:
                    event_index = index
                    break
        segment_units[event_index] += event.units

    for segment_index, (segment_plan, start, end) in enumerate(segments):
        segment_days = (end - start).days
        amount = round_half_up_cents(
            Decimal(segment_plan.monthly_price_cents) * Decimal(segment_days) / Decimal(period_days)
        )
        line_items.append(LineItem("plan", f"Plan: {segment_plan.name}", amount))

    subtotal_cents = sum(item.amount_cents for item in line_items)
    for segment_index, (segment_plan, start, end) in enumerate(segments):
        segment_days = (end - start).days
        included = segment_plan.included_units * segment_days // period_days
        overage_units = max(0, segment_units[segment_index] - included)
        if overage_units > 0:
            amount = overage_units * segment_plan.overage_unit_price_cents
            line_items.append(LineItem("overage", f"Overage: {segment_plan.name}", amount))
            subtotal_cents += amount

    discount_cents = compute_discount_cents(subtotal_cents, discount)
    if discount_cents:
        line_items.append(LineItem("discount", "Discount", -discount_cents))

    credit_cents = min(max(customer.credit_balance_cents, 0), max(subtotal_cents - discount_cents, 0))
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
