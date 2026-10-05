from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, Plan, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        # Resolve first, then honor idempotency before validating ignored payloads.
        self._store.get_subscription(subscription_id)
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.add_usage_event(
            subscription_id,
            UsageEvent(event_id=event_id, units=units, occurred_on=occurred_on),
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve the requested plan before mutating any state.
        self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        if not (
            subscription.period_start < effective_on < subscription.period_end
        ):
            raise ValueError("effective_on must be within the open billing period")
        if changes and effective_on <= changes[-1].effective_on:
            raise ValueError("plan changes must be strictly chronological")
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(effective_on=effective_on, plan_id=new_plan_id)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        changes = self._store.get_plan_changes(subscription_id)
        segments: list[tuple[Plan, int]] = []
        segment_start = subscription.period_start
        current_plan_id = subscription.plan_id
        for change in changes:
            segment_days = (change.effective_on - segment_start).days
            segments.append((self._store.get_plan(current_plan_id), segment_days))
            segment_start = change.effective_on
            current_plan_id = change.plan_id
        segments.append(
            (
                self._store.get_plan(current_plan_id),
                (subscription.period_end - segment_start).days,
            )
        )

        due_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments[0][0],
            customer,
            discount,
            segments=segments,
            usage_events=due_events,
            plan_changes=changes,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        if segments:
            subscription.plan_id = segments[-1][0].plan_id
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        if due_events:
            self._store.mark_usage_events_billed(
                subscription_id, {event.event_id for event in due_events}
            )
        self._store.clear_plan_changes(subscription_id)

        self._store.save_invoice(invoice)
        return invoice
