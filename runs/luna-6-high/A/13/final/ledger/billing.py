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
        self._store.get_subscription(subscription_id)
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        if units <= 0:
            raise ValueError("units must be greater than 0")
        self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        changes = subscription.plan_changes
        previous_date = changes[-1].effective_on if changes else subscription.period_start
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > previous_date
        ):
            raise ValueError("effective_on must be strictly inside the period and changes")
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        changes.append(PlanChange(effective_on, new_plan.plan_id))

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
            segments.append((segment_start, change.effective_on, segment_plan))
            segment_start = change.effective_on
            segment_plan = self._store.get_plan(change.plan_id)
        segments.append((segment_start, subscription.period_end, segment_plan))

        eligible_events = [
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
            segments=segments,
            usage_events=eligible_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        subscription.plan_id = segment_plan.plan_id
        subscription.plan_changes.clear()

        self._store.save_invoice(invoice)
        self._store.mark_usage_events_billed(
            subscription_id, {event.event_id for event in eligible_events}
        )
        return invoice
