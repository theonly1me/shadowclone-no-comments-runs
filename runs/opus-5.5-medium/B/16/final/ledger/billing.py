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
            raise ValueError("effective_on must be inside the open billing period")
        if subscription.plan_changes:
            last_change = subscription.plan_changes[-1]
            if effective_on <= last_change.effective_on:
                raise ValueError("effective_on must be after the previous plan change")
            current_plan_id = last_change.plan_id
        else:
            current_plan_id = subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")
        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None
        segments = self._segments(subscription)
        usage_events = [
            event
            for event in self._store.list_usage_events(subscription_id)
            if event.billed_invoice_id is None
            and event.occurred_on < subscription.period_end
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
            event.billed_invoice_id = invoice.invoice_id

        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes = []

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _segments(self, subscription: Subscription) -> list[Segment]:
        plan_ids = [subscription.plan_id]
        boundaries = [subscription.period_start]
        for change in subscription.plan_changes:
            plan_ids.append(change.plan_id)
            boundaries.append(change.effective_on)
        boundaries.append(subscription.period_end)
        return [
            Segment(self._store.get_plan(plan_id), boundaries[i], boundaries[i + 1])
            for i, plan_id in enumerate(plan_ids)
        ]
