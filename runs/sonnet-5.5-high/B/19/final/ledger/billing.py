from dataclasses import replace
from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, PlanChange, Subscription, UsageEvent
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
            UsageEvent(event_id, subscription_id, units, occurred_on)
        )

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

        subscription.plan_changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = self._segments(subscription)
        events = self._store.get_unbilled_usage_events(
            subscription_id, subscription.period_end
        )
        segments = self._attribute_usage(segments, events)

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.billed = True

        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes.clear()

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _segments(self, subscription: Subscription) -> list[Segment]:
        starts = [subscription.period_start] + [
            change.effective_on for change in subscription.plan_changes
        ]
        ends = starts[1:] + [subscription.period_end]
        plan_ids = [subscription.plan_id] + [
            change.plan_id for change in subscription.plan_changes
        ]
        return [
            Segment(self._store.get_plan(plan_id), start, end)
            for plan_id, start, end in zip(plan_ids, starts, ends)
        ]

    @staticmethod
    def _attribute_usage(
        segments: list[Segment], events: list[UsageEvent]
    ) -> list[Segment]:
        totals = [0] * len(segments)
        for event in events:
            index = 0
            for i, segment in enumerate(segments):
                if event.occurred_on >= segment.start:
                    index = i
            totals[index] += event.units
        return [replace(segment, units=units) for segment, units in zip(segments, totals)]
