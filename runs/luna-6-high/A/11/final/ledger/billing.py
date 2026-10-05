from datetime import date

from ledger.invoices import build_segmented_invoice
from ledger.models import Invoice, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        if units <= 0:
            raise ValueError("units must be greater than 0")
        self._store.get_subscription(subscription_id)
        return self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve the plan before mutating anything; unknown plans raise KeyError.
        self._store.get_plan(new_plan_id)
        if not (subscription.period_start < effective_on < subscription.period_end):
            raise ValueError("effective_on must be within the open billing period")
        if subscription.plan_changes and effective_on <= subscription.plan_changes[-1][0]:
            raise ValueError("plan changes must be strictly chronological")
        current_plan_id = (
            subscription.plan_changes[-1][1]
            if subscription.plan_changes
            else subscription.plan_id
        )
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        subscription.plan_changes.append((effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        period_start, period_end = subscription.period_start, subscription.period_end
        changes = list(subscription.plan_changes)
        boundaries = [(period_start, subscription.plan_id)] + changes
        segments = []
        for index, (segment_start, plan_id) in enumerate(boundaries):
            segment_end = boundaries[index + 1][0] if index + 1 < len(boundaries) else period_end
            plan = self._store.get_plan(plan_id)
            segments.append([segment_start, segment_end, plan, 0])

        eligible_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not event.billed and event.occurred_on < period_end
        ]
        for event in eligible_events:
            # Late events belong to the first segment. Other events use [start, end).
            segment_index = 0
            if event.occurred_on >= period_start:
                for index, segment in enumerate(segments):
                    if segment[0] <= event.occurred_on < segment[1]:
                        segment_index = index
                        break
            segments[segment_index][3] += event.units

        invoice = build_segmented_invoice(
            self._store.next_invoice_id(),
            subscription,
            [tuple(segment) for segment in segments],
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent
        for event in eligible_events:
            event.billed = True

        if changes:
            subscription.plan_id = changes[-1][1]
        subscription.plan_changes.clear()

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
