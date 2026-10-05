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
        if event_id in subscription.usage_events:
            return False
        if units <= 0:
            raise ValueError("Usage units must be greater than zero")
        subscription.usage_events[event_id] = UsageEvent(event_id, units, occurred_on)
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)
        changes = subscription.plan_changes
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        previous_date = changes[-1].effective_on if changes else subscription.period_start
        if not previous_date < effective_on < subscription.period_end:
            raise ValueError("Plan changes must be strictly increasing within the period")
        if new_plan_id == current_plan_id:
            raise ValueError("The new plan must differ from the current plan")
        changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = []
        segment_start = subscription.period_start
        segment_plan = plan
        for change in subscription.plan_changes:
            segments.append((segment_plan, segment_start, change.effective_on))
            segment_plan = self._store.get_plan(change.plan_id)
            segment_start = change.effective_on
        segments.append((segment_plan, segment_start, subscription.period_end))
        usage_events = [
            event
            for event_id, event in subscription.usage_events.items()
            if event_id not in subscription.billed_event_ids
            and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            segments=segments,
            usage_events=usage_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        subscription.billed_event_ids.update(event.event_id for event in usage_events)
        subscription.plan_id = segment_plan.plan_id
        subscription.plan_changes.clear()
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
