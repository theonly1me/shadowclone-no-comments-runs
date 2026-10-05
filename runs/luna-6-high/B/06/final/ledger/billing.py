from datetime import date, timedelta

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
        events = self._store.get_usage_events(subscription_id)
        if event_id in events:
            return False
        if units <= 0:
            raise ValueError("units must be greater than zero")
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
            raise ValueError("effective_on must be within the period and increasing")
        current_plan_id = previous_change.plan_id if previous_change else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(effective_on, new_plan.plan_id)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        changes = list(self._store.get_plan_changes(subscription_id))
        events = self._store.get_usage_events(subscription_id)
        segments = []
        segment_bounds = []
        segment_start = subscription.period_start
        segment_plan = plan
        for change in changes:
            segments.append(
                (segment_plan, (change.effective_on - segment_start).days)
            )
            segment_bounds.append((segment_start, change.effective_on))
            segment_start = change.effective_on
            segment_plan = self._store.get_plan(change.plan_id)
        segments.append((segment_plan, (subscription.period_end - segment_start).days))
        segment_bounds.append((segment_start, subscription.period_end))

        eligible_events = [
            event
            for event in events.values()
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        segment_units = [0] * len(segments)
        for event in eligible_events:
            event_date = max(event.occurred_on, subscription.period_start)
            for index, (start, end) in enumerate(segment_bounds):
                if start <= event_date < end:
                    segment_units[index] += event.units
                    break

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

        for event in eligible_events:
            event.billed = True

        if changes:
            subscription.plan_id = changes[-1].plan_id
        self._store.clear_plan_changes(subscription_id)

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
