from datetime import date

from ledger.invoices import PlanSegment, build_invoice
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
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve the target before changing anything so unknown plans fail atomically.
        self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        previous_date = changes[-1].effective_on if changes else subscription.period_start
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > previous_date
        ):
            raise ValueError("effective_on must be strictly increasing within the period")
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(effective_on, new_plan_id)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        changes = self._store.get_plan_changes(subscription_id)
        segments: list[PlanSegment] = []
        segment_start = subscription.period_start
        plan_id = subscription.plan_id
        for change in changes:
            segments.append(
                PlanSegment(segment_start, change.effective_on, self._store.get_plan(plan_id))
            )
            segment_start = change.effective_on
            plan_id = change.plan_id
        segments.append(
            PlanSegment(segment_start, subscription.period_end, self._store.get_plan(plan_id))
        )

        billable_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            billable_events,
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        # Changes apply to this period only; their final plan carries forward.
        subscription.plan_id = plan_id
        self._store.clear_plan_changes(subscription_id)
        self._store.remove_usage_events(
            subscription_id, {event.event_id for event in billable_events}
        )

        self._store.save_invoice(invoice)
        return invoice
