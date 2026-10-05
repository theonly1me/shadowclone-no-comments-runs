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
        subscription = self._store.get_subscription(subscription_id)
        if any(event.event_id == event_id for event in subscription.usage_events):
            return False
        if units <= 0:
            raise ValueError("units must be greater than zero")
        subscription.usage_events.append(UsageEvent(event_id, units, occurred_on))
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(new_plan_id)
        last_effective_on = (
            subscription.plan_changes[-1].effective_on
            if subscription.plan_changes
            else subscription.period_start
        )
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > last_effective_on
        ):
            raise ValueError(
                "effective_on must be within the open period and increasing"
            )
        current_plan_id = (
            subscription.plan_changes[-1].plan_id
            if subscription.plan_changes
            else subscription.plan_id
        )
        if plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        subscription.plan_changes.append(PlanChange(plan.plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        events = [
            event
            for event in subscription.usage_events
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            self._store,
            customer,
            discount,
            events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.billed = True

        if subscription.plan_changes:
            subscription.plan_id = subscription.plan_changes[-1].plan_id
            subscription.plan_changes.clear()

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
