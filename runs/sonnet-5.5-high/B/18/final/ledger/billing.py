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
        event = UsageEvent(event_id, subscription_id, units, occurred_on)
        return self._store.add_usage_event(event)

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        changes = subscription.plan_changes
        if changes and effective_on <= changes[-1].effective_on:
            raise ValueError("effective_on must be after the previous plan change")
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")
        changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = self._build_segments(subscription)
        events = [
            event
            for event in self._store.get_unbilled_usage(subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        for event in events:
            target = segments[0]
            for segment in segments:
                if segment.start <= event.occurred_on < segment.end:
                    target = segment
                    break
            target.units += event.units

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.billed = True

        length = subscription.period_end - subscription.period_start
        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes = []
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _build_segments(self, subscription) -> list[Segment]:
        boundaries = [(subscription.period_start, subscription.plan_id)]
        boundaries += [(c.effective_on, c.plan_id) for c in subscription.plan_changes]
        segments = []
        for index, (start, plan_id) in enumerate(boundaries):
            if index + 1 < len(boundaries):
                end = boundaries[index + 1][0]
            else:
                end = subscription.period_end
            segments.append(Segment(self._store.get_plan(plan_id), start, end))
        return segments
