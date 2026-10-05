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
            raise ValueError("units must be greater than 0")
        subscription.usage_events.append(UsageEvent(event_id, units, occurred_on))
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must be within the open period")
        if subscription.plan_changes:
            last_change = subscription.plan_changes[-1]
            if effective_on <= last_change.effective_on:
                raise ValueError("plan changes must be strictly increasing")
            current_plan_id = last_change.plan_id
        else:
            current_plan_id = subscription.plan_id
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        subscription.plan_changes.append(PlanChange(new_plan.plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segment_starts = [subscription.period_start]
        segment_plans = [self._store.get_plan(subscription.plan_id)]
        for change in subscription.plan_changes:
            segment_starts.append(change.effective_on)
            segment_plans.append(self._store.get_plan(change.plan_id))
        segments = [
            (segment_plans[index], segment_starts[index], segment_starts[index + 1])
            for index in range(len(segment_starts) - 1)
        ]
        segments.append(
            (segment_plans[-1], segment_starts[-1], subscription.period_end)
        )
        usage_events = [
            event
            for event in subscription.usage_events
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segment_plans[0],
            customer,
            discount,
            segments,
            usage_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage_events:
            event.billed = True

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        subscription.plan_id = segment_plans[-1].plan_id
        subscription.plan_changes.clear()

        self._store.save_invoice(invoice)
        return invoice
