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
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = []
        start = subscription.period_start
        plan_id = subscription.plan_id
        for change in subscription.plan_changes:
            segments.append(Segment(self._store.get_plan(plan_id), start, change.effective_on))
            start = change.effective_on
            plan_id = change.plan_id
        segments.append(Segment(self._store.get_plan(plan_id), start, subscription.period_end))

        events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, events, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.billed = True

        subscription.plan_id = plan_id
        subscription.plan_changes = []

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
