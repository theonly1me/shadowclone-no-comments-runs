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
            UsageEvent(event_id, subscription_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        if (
            subscription.plan_changes
            and effective_on <= subscription.plan_changes[-1][0]
        ):
            raise ValueError("effective_on must be after the previous plan change")
        if new_plan_id == self._current_plan_id(subscription):
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append((effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None
        segments = self._segments(subscription)
        usage_events = self._store.unbilled_usage_events(
            subscription_id, subscription.period_end
        )

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            usage_events,
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage_events:
            event.billed = True

        subscription.plan_id = self._current_plan_id(subscription)
        subscription.plan_changes.clear()

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _current_plan_id(self, subscription: Subscription) -> str:
        if subscription.plan_changes:
            return subscription.plan_changes[-1][1]
        return subscription.plan_id

    def _segments(self, subscription: Subscription) -> list[Segment]:
        segments = []
        start = subscription.period_start
        plan_id = subscription.plan_id
        for effective_on, next_plan_id in subscription.plan_changes:
            segments.append(Segment(self._store.get_plan(plan_id), start, effective_on))
            start = effective_on
            plan_id = next_plan_id
        segments.append(
            Segment(self._store.get_plan(plan_id), start, subscription.period_end)
        )
        return segments
