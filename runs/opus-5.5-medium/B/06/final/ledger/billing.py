from dataclasses import replace
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
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall strictly inside the open period")
        changes = subscription.plan_changes
        if changes and effective_on <= changes[-1].effective_on:
            raise ValueError("effective_on must be after the previous plan change")
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")
        changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        segments = self._segments(subscription, events)

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
        subscription.plan_changes = []

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _segments(
        self, subscription: Subscription, events: list[UsageEvent]
    ) -> list[Segment]:
        boundaries = [(subscription.period_start, subscription.plan_id)]
        boundaries += [(c.effective_on, c.plan_id) for c in subscription.plan_changes]
        segments = []
        for index, (start, plan_id) in enumerate(boundaries):
            end = (
                boundaries[index + 1][0]
                if index + 1 < len(boundaries)
                else subscription.period_end
            )
            segments.append(Segment(self._store.get_plan(plan_id), start, end))

        units = [0] * len(segments)
        for event in events:
            index = 0
            for position, segment in enumerate(segments):
                if segment.start <= event.occurred_on:
                    index = position
            units[index] += event.units
        return [replace(segment, units=units[i]) for i, segment in enumerate(segments)]
