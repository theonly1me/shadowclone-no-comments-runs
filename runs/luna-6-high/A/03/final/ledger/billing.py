from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self._store.get_subscription(subscription_id)
        # A duplicate is ignored completely, including validation of its new payload.
        if any(
            event.event_id == event_id
            for event in self._store.get_usage_events(subscription_id)
        ):
            return False
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.add_usage_event(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        previous_date = changes[-1].effective_on if changes else subscription.period_start
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > previous_date
        ):
            raise ValueError("effective_on must be strictly inside the open period and ordered")
        if new_plan.plan_id == current_plan_id:
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
        boundaries = [subscription.period_start] + [
            change.effective_on for change in changes
        ] + [subscription.period_end]
        segment_plan_ids = [subscription.plan_id] + [
            change.plan_id for change in changes
        ]
        segment_plans = [self._store.get_plan(plan_id) for plan_id in segment_plan_ids]

        eligible_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not self._store.is_usage_event_billed(subscription_id, event.event_id)
            and event.occurred_on < subscription.period_end
        ]
        segment_units = [0] * len(segment_plans)
        for event in eligible_events:
            if event.occurred_on < subscription.period_start:
                index = 0
            else:
                index = next(
                    i
                    for i in range(len(segment_plans))
                    if boundaries[i] <= event.occurred_on < boundaries[i + 1]
                )
            segment_units[index] += event.units

        segments = [
            (segment_plan, (boundaries[i + 1] - boundaries[i]).days, segment_units[i])
            for i, segment_plan in enumerate(segment_plans)
        ]
        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, plan, customer, discount, segments
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        self._store.mark_usage_events_billed(
            subscription_id, {event.event_id for event in eligible_events}
        )

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        if changes:
            subscription.plan_id = changes[-1].plan_id
        self._store.clear_plan_changes(subscription_id)
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
