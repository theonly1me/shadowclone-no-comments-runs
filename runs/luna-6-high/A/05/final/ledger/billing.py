from datetime import date, timedelta

from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        # Resolve the subscription first so unknown IDs consistently raise KeyError.
        self._store.get_subscription(subscription_id)
        return self._store.record_usage(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        changes = self._store.get_plan_changes(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        previous_change = changes[-1] if changes else None
        previous_plan_id = previous_change.plan_id if previous_change else subscription.plan_id
        if new_plan_id == previous_plan_id:
            raise ValueError("plan is already current")
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must be within the open period")
        if previous_change and effective_on <= previous_change.effective_on:
            raise ValueError("plan changes must be strictly chronological")
        self._store.add_plan_change(
            subscription_id, PlanChange(effective_on, new_plan.plan_id)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        changes = list(self._store.get_plan_changes(subscription_id))
        segments = []
        segment_start = subscription.period_start
        for change in changes:
            segments.append(
                (self._store.get_plan(plan.plan_id), segment_start, change.effective_on)
            )
            plan = self._store.get_plan(change.plan_id)
            segment_start = change.effective_on
        segments.append((plan, segment_start, subscription.period_end))
        usage_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, plan, customer, discount,
            segments=segments, usage_events=usage_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        if changes:
            subscription.plan_id = changes[-1].plan_id
            self._store.clear_plan_changes(subscription_id)
        self._store.consume_usage_events(
            subscription_id, {event.event_id for event in usage_events}
        )

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
