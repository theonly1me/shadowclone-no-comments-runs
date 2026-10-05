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
        # Resolve the subscription before inspecting event data so unknown IDs
        # consistently raise the store's KeyError.
        self._store.get_subscription(subscription_id)
        existing = {
            event.event_id for event in self._store.get_usage_events(subscription_id)
        }
        if event_id in existing:
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
        new_plan = self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        previous_change_on = changes[-1].effective_on if changes else None

        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or (previous_change_on is not None and effective_on <= previous_change_on)
        ):
            raise ValueError("effective_on must be strictly increasing within the period")
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(plan_id=new_plan_id, effective_on=effective_on)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        plan_changes = [
            (change, self._store.get_plan(change.plan_id))
            for change in self._store.get_plan_changes(subscription_id)
        ]
        usage_events = self._store.get_usage_events(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            plan_changes=plan_changes,
            usage_events=usage_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        billed_event_ids = {
            event.event_id
            for event in usage_events
            if not event.billed and event.occurred_on < subscription.period_end
        }
        self._store.mark_usage_events_billed(subscription_id, billed_event_ids)

        if plan_changes:
            subscription.plan_id = plan_changes[-1][0].plan_id
        self._store.clear_plan_changes(subscription_id)

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
