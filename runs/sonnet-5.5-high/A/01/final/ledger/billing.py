from datetime import date, timedelta

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self._store.get_subscription(subscription_id)  # KeyError if unknown
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.add_usage_event(
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)  # KeyError if unknown

        previous = (
            subscription.plan_changes[-1].effective_on
            if subscription.plan_changes
            else subscription.period_start
        )
        if not (previous < effective_on < subscription.period_end):
            raise ValueError("effective_on is outside the allowed range")
        current_plan_id = (
            subscription.plan_changes[-1].plan_id
            if subscription.plan_changes
            else subscription.plan_id
        )
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        boundaries = [(subscription.period_start, subscription.plan_id)] + [
            (change.effective_on, change.plan_id) for change in subscription.plan_changes
        ]
        segments = [
            Segment(start, end, self._store.get_plan(plan_id))
            for (start, plan_id), end in zip(
                boundaries,
                [start for start, _ in boundaries[1:]] + [subscription.period_end],
            )
        ]
        usage_events = self._store.get_unbilled_usage(
            subscription_id, subscription.period_end
        )

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            customer,
            discount,
            usage_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage_events:
            event.billed = True

        subscription.plan_id = boundaries[-1][1]
        subscription.plan_changes.clear()

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
