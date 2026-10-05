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
        subscription = self._store.get_subscription(subscription_id)
        if units <= 0:
            raise ValueError("units must be greater than 0")
        if event_id in subscription.usage_events:
            return False
        subscription.usage_events[event_id] = UsageEvent(event_id, units, occurred_on)
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)
        changes = subscription.plan_changes
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        floor = changes[-1].effective_on if changes else subscription.period_start
        if effective_on <= floor or effective_on >= subscription.period_end:
            raise ValueError("effective_on is outside the allowed range")
        if new_plan_id == current_plan_id:
            raise ValueError("plan is already current")
        changes.append(PlanChange(new_plan_id, effective_on))

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
        segments = [
            Segment(self._store.get_plan(plan_id), start, end)
            for plan_id, start, end in zip(plan_ids, starts, ends)
        ]

        usage_events = [
            event
            for event in subscription.usage_events.values()
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            usage_events,
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage_events:
            event.billed = True

        subscription.plan_id = plan_ids[-1]
        subscription.plan_changes = []

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
