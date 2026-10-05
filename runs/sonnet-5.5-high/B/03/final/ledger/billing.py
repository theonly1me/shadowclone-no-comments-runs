from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, Segment, Subscription, UsageEvent
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
        if subscription.plan_changes and effective_on <= subscription.plan_changes[-1].effective_on:
            raise ValueError("effective_on must be after the previous plan change")
        if new_plan_id == self._current_plan_id(subscription):
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = self._build_segments(subscription)
        events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        for event in events:
            self._segment_for(segments, event.occurred_on).units += event.units

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

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _current_plan_id(self, subscription: Subscription) -> str:
        if subscription.plan_changes:
            return subscription.plan_changes[-1].plan_id
        return subscription.plan_id

    def _build_segments(self, subscription: Subscription) -> list[Segment]:
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

    def _segment_for(self, segments: list[Segment], day: date) -> Segment:
        for segment in segments:
            if segment.start <= day < segment.end:
                return segment
        return segments[0]
