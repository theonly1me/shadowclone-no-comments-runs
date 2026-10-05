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
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )

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

        changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        events = self._store.get_unbilled_usage(subscription_id, subscription.period_end)
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

        length = subscription.period_end - subscription.period_start
        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes = []
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _segments(
        self, subscription: Subscription, events: list[UsageEvent]
    ) -> list[Segment]:
        starts = [subscription.period_start]
        plan_ids = [subscription.plan_id]
        for change in subscription.plan_changes:
            starts.append(change.effective_on)
            plan_ids.append(change.plan_id)
        ends = starts[1:] + [subscription.period_end]

        segments = []
        for index, (plan_id, start, end) in enumerate(zip(plan_ids, starts, ends)):
            units = sum(
                event.units
                for event in events
                if start <= event.occurred_on < end
                or (index == 0 and event.occurred_on < start)
            )
            segments.append(
                Segment(self._store.get_plan(plan_id), start, end, units)
            )
        return segments
