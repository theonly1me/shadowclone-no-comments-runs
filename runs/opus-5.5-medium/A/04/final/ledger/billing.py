from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, PlanChange, Subscription, UsageEvent
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
        # Events are idempotent per subscription: a repeated event_id is ignored.
        if self._store.get_usage_event(subscription_id, event_id) is not None:
            return False
        self._store.add_usage_event(
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must be strictly inside the open period")
        if subscription.plan_changes:
            last = subscription.plan_changes[-1]
            if effective_on <= last.effective_on:
                raise ValueError("effective_on must be after the previous plan change")
        if new_plan_id == self._current_plan_id(subscription):
            raise ValueError(f"subscription is already on plan {new_plan_id}")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = self._segments(subscription)
        events = [
            event
            for event in self._store.list_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        for event in events:
            self._segment_for(segments, event.occurred_on).units += event.units

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
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
            return subscription.plan_changes[-1].plan_id
        return subscription.plan_id

    def _segments(self, subscription: Subscription) -> list[Segment]:
        boundaries = [(subscription.plan_id, subscription.period_start)]
        boundaries += [(c.plan_id, c.effective_on) for c in subscription.plan_changes]
        ends = [start for _, start in boundaries[1:]] + [subscription.period_end]
        return [
            Segment(self._store.get_plan(plan_id), start, end)
            for (plan_id, start), end in zip(boundaries, ends)
        ]

    @staticmethod
    def _segment_for(segments: list[Segment], day: date) -> Segment:
        # Late events from a closed period fall before the first segment.
        for segment in reversed(segments):
            if segment.start <= day:
                return segment
        return segments[0]
