from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        changes = subscription.plan_changes
        segments = []
        current_plan_id = subscription.plan_id
        segment_start = subscription.period_start
        for change in changes:
            segments.append(
                (
                    self._store.get_plan(current_plan_id),
                    segment_start.toordinal(),
                    change.effective_on.toordinal(),
                )
            )
            current_plan_id = change.plan_id
            segment_start = change.effective_on
        segments.append(
            (
                self._store.get_plan(current_plan_id),
                segment_start.toordinal(),
                subscription.period_end.toordinal(),
            )
        )

        billable_events = [
            event
            for event in subscription.usage_events
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            billable_events,
            customer,
            discount,
        )

        for event in billable_events:
            event.billed = True

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        subscription.plan_id = current_plan_id
        subscription.plan_changes.clear()

        self._store.save_invoice(invoice)
        return invoice

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        subscription = self._store.get_subscription(subscription_id)
        if any(event.event_id == event_id for event in subscription.usage_events):
            return False
        if units <= 0:
            raise ValueError("units must be greater than 0")
        subscription.usage_events.append(UsageEvent(event_id, units, occurred_on))
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(new_plan_id)
        previous_change = subscription.plan_changes[-1] if subscription.plan_changes else None
        current_plan_id = previous_change.plan_id if previous_change else subscription.plan_id
        previous_date = previous_change.effective_on if previous_change else subscription.period_start

        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or effective_on <= previous_date
        ):
            raise ValueError("effective_on must be after the previous period boundary")
        if plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))
