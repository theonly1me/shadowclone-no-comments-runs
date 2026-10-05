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
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        self._store.add_usage_event(
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        if subscription.plan_changes:
            last = subscription.plan_changes[-1]
            if effective_on <= last.effective_on:
                raise ValueError("effective_on must be after the previous change")
            current_plan_id = last.plan_id
        else:
            current_plan_id = subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("plan is already current")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        starts = [subscription.period_start] + [
            change.effective_on for change in subscription.plan_changes
        ]
        ends = starts[1:] + [subscription.period_end]
        plan_ids = [subscription.plan_id] + [
            change.plan_id for change in subscription.plan_changes
        ]
        units = [0] * len(starts)

        events = [
            event
            for event in self._store.unbilled_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        for event in events:
            index = 0
            for i, start in enumerate(starts):
                if start <= event.occurred_on:
                    index = i
            units[index] += event.units

        segments = [
            Segment(self._store.get_plan(plan_id), start, end, segment_units)
            for plan_id, start, end, segment_units in zip(plan_ids, starts, ends, units)
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
