from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        subscription = self._store.get_subscription(subscription_id)
        if units <= 0:
            raise ValueError("units must be greater than 0")
        if event_id in subscription.usage_events:
            return False
        subscription.usage_events[event_id] = UsageEvent(event_id, units, occurred_on)
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)  # KeyError if unknown

        floor = subscription.period_start
        current_plan_id = subscription.plan_id
        if subscription.plan_changes:
            floor = subscription.plan_changes[-1].effective_on
            current_plan_id = subscription.plan_changes[-1].plan_id
        if not floor < effective_on < subscription.period_end:
            raise ValueError("effective_on is outside the allowed range")
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None


        segments = []
        start = subscription.period_start
        segment_plan = plan
        for change in subscription.plan_changes:
            segments.append(Segment(segment_plan, start, change.effective_on))
            start = change.effective_on
            segment_plan = self._store.get_plan(change.plan_id)
        segments.append(Segment(segment_plan, start, subscription.period_end))

        usage_events = [
            event
            for event in subscription.usage_events.values()
            if not event.billed and event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
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
        subscription.plan_id = segment_plan.plan_id
        subscription.plan_changes.clear()

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
