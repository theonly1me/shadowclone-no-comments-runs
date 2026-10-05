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
        self._store.get_subscription(subscription_id)
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        if units <= 0:
            raise ValueError("units must be greater than zero")
        return self._store.record_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id

        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or (changes and effective_on <= changes[-1].effective_on)
        ):
            raise ValueError("plan change date must be ordered within the open period")
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")

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
        plan_changes = self._store.get_plan_changes(subscription_id)
        plans = {
            change.plan_id: self._store.get_plan(change.plan_id)
            for change in plan_changes
        }
        usage_events = [
            event
            for event in self._store.get_unbilled_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            plan_changes,
            usage_events,
            plans,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        if plan_changes:
            subscription.plan_id = plan_changes[-1].plan_id
        self._store.clear_plan_changes(subscription_id)
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.mark_usage_events_billed(
            subscription_id, {event.event_id for event in usage_events}
        )
        self._store.save_invoice(invoice)
        return invoice
