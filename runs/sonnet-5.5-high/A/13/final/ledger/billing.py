from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, Subscription, UsageEvent
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
        self._store.get_plan(new_plan_id)

        last_change_on = (
            subscription.plan_changes[-1][0]
            if subscription.plan_changes
            else subscription.period_start
        )
        if not (
            last_change_on < effective_on < subscription.period_end
        ):
            raise ValueError("effective_on is outside the allowed range")
        if new_plan_id == self._current_plan_id(subscription):
            raise ValueError("subscription is already on that plan")

        subscription.plan_changes.append((effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        # Segment boundaries: (start, plan_id), the first one using the plan the
        # period started with.
        starts = [(subscription.period_start, subscription.plan_id)]
        starts.extend(subscription.plan_changes)
        ends = [start for start, _ in starts[1:]] + [subscription.period_end]
        segments = [
            [self._store.get_plan(plan_id), (end - start).days, 0]
            for (start, plan_id), end in zip(starts, ends)
        ]

        events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        for event in events:
            index = 0  # late events land in the first segment
            for i, (start, _) in enumerate(starts):
                if start <= event.occurred_on:
                    index = i
            segments[index][2] += event.units

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            [Segment(*segment) for segment in segments],
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent
        for event in events:
            event.billed = True

        subscription.plan_id = self._current_plan_id(subscription)
        subscription.plan_changes = []

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    @staticmethod
    def _current_plan_id(subscription: Subscription) -> str:
        if subscription.plan_changes:
            return subscription.plan_changes[-1][1]
        return subscription.plan_id
