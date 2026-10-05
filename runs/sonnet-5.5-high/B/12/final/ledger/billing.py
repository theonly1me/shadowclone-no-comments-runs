from bisect import bisect_right
from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self._store.get_subscription(subscription_id)
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.add_usage_event(
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        if (
            subscription.plan_changes
            and effective_on <= subscription.plan_changes[-1].effective_on
        ):
            raise ValueError("effective_on must be after the previous plan change")
        current_plan_id = (
            subscription.plan_changes[-1].plan_id
            if subscription.plan_changes
            else subscription.plan_id
        )
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        starts = [subscription.period_start]
        plan_ids = [subscription.plan_id]
        for change in subscription.plan_changes:
            starts.append(change.effective_on)
            plan_ids.append(change.plan_id)
        plans = [self._store.get_plan(plan_id) for plan_id in plan_ids]
        ends = starts[1:] + [subscription.period_end]

        units = [0] * len(plans)
        events = [
            event
            for event in self._store.get_unbilled_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        for event in events:
            index = max(0, bisect_right(starts, event.occurred_on) - 1)
            units[index] += event.units

        segments = [
            Segment(plan, (end - start).days, segment_units)
            for plan, start, end, segment_units in zip(plans, starts, ends, units)
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.billed = True

        subscription.plan_id = plan_ids[-1]
        subscription.plan_changes = []

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
