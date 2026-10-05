from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(self, subscription_id: str, event_id: str, units: int, occurred_on: date) -> bool:
        self._store.get_subscription(subscription_id)
        if units <= 0:
            raise ValueError("units must be greater than 0")
        events = self._store.get_usage_events(subscription_id)
        if any(event.event_id == event_id for event in events):
            return False
        self._store.add_usage_event(subscription_id, UsageEvent(event_id, units, occurred_on))
        return True

    def change_plan(self, subscription_id: str, new_plan_id: str, effective_on: date) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        previous_date = changes[-1].effective_on if changes else subscription.period_start
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must be within the open period")
        if effective_on <= previous_date:
            raise ValueError("plan changes must be strictly chronological")
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(subscription_id, PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None
        changes = self._store.get_plan_changes(subscription_id)
        events = self._store.get_usage_events(subscription_id)
        plans = {change.plan_id: self._store.get_plan(change.plan_id) for change in changes}

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, plan, customer, discount,
            changes, events, plans,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        billable_event_ids = {
            event.event_id for event in events
            if not event.billed and event.occurred_on < subscription.period_end
        }
        final_plan_id = changes[-1].plan_id if changes else subscription.plan_id

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        subscription.plan_id = final_plan_id
        self._store.mark_usage_events_billed(subscription_id, billable_event_ids)
        self._store.clear_plan_changes(subscription_id)

        self._store.save_invoice(invoice)
        return invoice
