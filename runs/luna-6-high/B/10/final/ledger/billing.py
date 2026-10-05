from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, Plan, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        changes = list(self._store.get_plan_changes(subscription_id))
        segments: list[tuple[Plan, int, int]] = []
        segment_starts = [subscription.period_start] + [
            change.effective_on for change in changes
        ]
        segment_ends = [change.effective_on for change in changes] + [
            subscription.period_end
        ]
        segment_plan_ids = [subscription.plan_id] + [
            change.plan_id for change in changes
        ]
        segment_plans = [self._store.get_plan(plan_id) for plan_id in segment_plan_ids]
        events = self._store.get_usage_events(subscription_id)
        billed_events = {
            event_id: event
            for event_id, event in events.items()
            if event.occurred_on < subscription.period_end
        }
        units_per_segment = [0] * len(segment_plans)
        for event in billed_events.values():
            segment_index = 0
            if event.occurred_on >= subscription.period_start:
                for index, end in enumerate(segment_ends):
                    if event.occurred_on < end:
                        segment_index = index
                        break
            units_per_segment[segment_index] += event.units
        for index, segment_plan in enumerate(segment_plans):
            segment_days = (segment_ends[index] - segment_starts[index]).days
            segments.append((segment_plan, segment_days, units_per_segment[index]))

        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segment_plans[0],
            customer,
            discount,
            segments,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        subscription.plan_id = segment_plan_ids[-1]
        for event_id in billed_events:
            del events[event_id]
        self._store.clear_plan_changes(subscription_id)

        self._store.save_invoice(invoice)
        return invoice

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        if units <= 0:
            raise ValueError("units must be greater than zero")
        self._store.get_subscription(subscription_id)
        events = self._store.get_usage_events(subscription_id)
        if event_id in events:
            return False
        self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        previous_change = changes[-1] if changes else None
        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or (previous_change and effective_on <= previous_change.effective_on)
        ):
            raise ValueError("effective_on must be within the open period and ordered")
        current_plan_id = (
            previous_change.plan_id if previous_change else subscription.plan_id
        )
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(new_plan.plan_id, effective_on)
        )
