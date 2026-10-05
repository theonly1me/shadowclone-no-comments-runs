from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice
from ledger.storage import InMemoryStore, PlanChange, UsageEvent


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self._store.get_subscription(subscription_id)
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.record_usage(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve the requested plan before mutating anything so unknown plans
        # consistently fail without leaving a partial change behind.
        self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        previous_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        previous_effective_on = (
            changes[-1].effective_on if changes else subscription.period_start
        )
        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or effective_on <= previous_effective_on
        ):
            raise ValueError("plan change date must be in order within the open period")
        if new_plan_id == previous_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(new_plan_id, effective_on)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        changes = self._store.get_plan_changes(subscription_id)
        segment_plans = [plan] + [
            self._store.get_plan(change.plan_id) for change in changes
        ]
        boundaries = [subscription.period_start] + [
            change.effective_on for change in changes
        ] + [subscription.period_end]
        segments = [
            (segment_plans[index], (boundaries[index + 1] - boundaries[index]).days)
            for index in range(len(segment_plans))
        ]

        due_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]
        segment_units = [0] * len(segments)
        for event in due_events:
            if event.occurred_on < subscription.period_start:
                segment_index = 0
            else:
                segment_index = next(
                    index
                    for index in range(len(segments))
                    if boundaries[index] <= event.occurred_on < boundaries[index + 1]
                )
            segment_units[segment_index] += event.units

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            segments,
            segment_units,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.plan_id = segment_plans[-1].plan_id
        self._store.clear_plan_changes(subscription_id)
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.remove_usage_events(
            subscription_id, {event.event_id for event in due_events}
        )

        self._store.save_invoice(invoice)
        return invoice
