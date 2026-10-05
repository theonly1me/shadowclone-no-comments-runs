from datetime import date
from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        if units <= 0:
            raise ValueError("units must be greater than 0")
        self._store.get_subscription(subscription_id)
        return self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        if not (
            subscription.period_start < effective_on < subscription.period_end
        ):
            raise ValueError("effective_on must be within the open period")
        if changes and effective_on <= changes[-1].effective_on:
            raise ValueError("plan changes must be strictly chronological")
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        eligible_events = [
            event for event in self._store.get_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            [(change.effective_on, self._store.get_plan(change.plan_id)) for change in changes],
            eligible_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        if changes:
            subscription.plan_id = changes[-1].plan_id
            changes.clear()
        self._store.mark_usage_events_billed(
            subscription_id, {event.event_id for event in eligible_events}
        )
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
