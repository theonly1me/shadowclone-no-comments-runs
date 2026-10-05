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
        # Events are idempotent: a repeated event_id is ignored entirely.
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("plan change must fall strictly inside the open period")
        if subscription.plan_changes:
            last = subscription.plan_changes[-1]
            if effective_on <= last.effective_on:
                raise ValueError("plan change must be after the previous change")
        if new_plan_id == self._current_plan_id(subscription):
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments = self._segments(subscription)
        billable = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        units = [0] * len(segments)
        for event in billable:
            units[self._segment_index(segments, event.occurred_on)] += event.units
        segments = [
            Segment(s.plan, s.start, s.end, segment_units)
            for s, segment_units in zip(segments, units)
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent
        for event in billable:
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
        segments = []
        plan_id = subscription.plan_id
        start = subscription.period_start
        for change in subscription.plan_changes:
            plan = self._store.get_plan(plan_id)
            segments.append(Segment(plan, start, change.effective_on))
            plan_id, start = change.plan_id, change.effective_on
        plan = self._store.get_plan(plan_id)
        segments.append(Segment(plan, start, subscription.period_end))
        return segments

    @staticmethod
    def _segment_index(segments: list[Segment], occurred_on: date) -> int:
        # Late events from a closed period land in the first segment.
        for index in range(len(segments) - 1, 0, -1):
            if occurred_on >= segments[index].start:
                return index
        return 0
