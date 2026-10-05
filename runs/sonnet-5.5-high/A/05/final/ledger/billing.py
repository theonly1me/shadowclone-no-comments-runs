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
        if units <= 0:
            raise ValueError("units must be greater than 0")
        self._store.get_subscription(subscription_id)  # KeyError if unknown
        event = UsageEvent(event_id, units, occurred_on)
        return self._store.add_usage_event(subscription_id, event)

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)  # KeyError if unknown

        previous = subscription.plan_changes[-1] if subscription.plan_changes else None
        earliest = previous.effective_on if previous else subscription.period_start
        if not earliest < effective_on < subscription.period_end:
            raise ValueError("effective_on is outside the allowed range")
        current_plan_id = previous.plan_id if previous else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")

        subscription.plan_changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        segments, usage = self._segments(subscription)
        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments[0].plan,
            customer,
            discount,
            segments,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage:
            event.billed = True
        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes.clear()

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice

    def _segments(self, subscription) -> tuple[list[Segment], list[UsageEvent]]:
        """Split the open period by plan changes and attribute unbilled usage."""
        starts = [subscription.period_start]
        plan_ids = [subscription.plan_id]
        for change in subscription.plan_changes:
            starts.append(change.effective_on)
            plan_ids.append(change.plan_id)
        ends = starts[1:] + [subscription.period_end]

        usage = [
            event
            for event in self._store.get_unbilled_usage(subscription.subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        units = [0] * len(starts)
        for event in usage:
            # Late events (before period_start) land in the first segment.
            index = 0
            for i, start in enumerate(starts):
                if event.occurred_on >= start:
                    index = i
            units[index] += event.units

        segments = [
            Segment(self._store.get_plan(plan_id), start, end, segment_units)
            for plan_id, start, end, segment_units in zip(plan_ids, starts, ends, units)
        ]
        return segments, usage
