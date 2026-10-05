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
        self._store.get_subscription(subscription_id)  # KeyError if unknown
        if units <= 0:
            raise ValueError("units must be greater than 0")
        event = UsageEvent(event_id, units, occurred_on)
        return self._store.add_usage_event(subscription_id, event)

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)  # KeyError if unknown

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        changes = subscription.plan_changes
        if changes and effective_on <= changes[-1].effective_on:
            raise ValueError("effective_on must be after the previous plan change")
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on that plan")

        changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        # Segment boundaries: (start, plan_id) for each stretch of the period.
        starts = [(subscription.period_start, subscription.plan_id)]
        starts += [(c.effective_on, c.plan_id) for c in subscription.plan_changes]
        ends = [start for start, _ in starts[1:]] + [subscription.period_end]

        # Events not yet billed whose occurred_on is before period_end. Late
        # events (before period_start) fall into the first segment.
        events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        segments = []
        for (start, plan_id), end in zip(starts, ends):
            first = start == subscription.period_start
            units = sum(
                event.units
                for event in events
                if event.occurred_on < end and (first or event.occurred_on >= start)
            )
            segments.append(
                Segment(self._store.get_plan(plan_id), (end - start).days, units)
            )

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.billed = True

        subscription.plan_id = starts[-1][1]
        subscription.plan_changes = []

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
