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
        if units <= 0:
            raise ValueError("units must be greater than 0")
        subscription = self._store.get_subscription(subscription_id)
        if any(event.event_id == event_id for event in subscription.usage_events):
            return False
        subscription.usage_events.append(
            UsageEvent(event_id=event_id, units=units, occurred_on=occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        new_plan = self._store.get_plan(new_plan_id)

        previous_change = (
            subscription.plan_changes[-1] if subscription.plan_changes else None
        )
        lower_bound = (
            previous_change.effective_on
            if previous_change is not None
            else subscription.period_start
        )
        current_plan_id = (
            previous_change.plan_id
            if previous_change is not None
            else subscription.plan_id
        )
        if not lower_bound < effective_on < subscription.period_end:
            raise ValueError("effective_on must be within the open billing period")
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        subscription.plan_changes.append(
            PlanChange(effective_on=effective_on, plan_id=new_plan_id)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        period_days = (subscription.period_end - subscription.period_start).days
        segment_starts = [subscription.period_start] + [
            change.effective_on for change in subscription.plan_changes
        ]
        segment_ends = [
            change.effective_on for change in subscription.plan_changes
        ] + [subscription.period_end]
        segment_plan_ids = [subscription.plan_id] + [
            change.plan_id for change in subscription.plan_changes
        ]
        segment_units = [0] * len(segment_starts)
        billable_events = [
            event
            for event in subscription.usage_events
            if not event.billed and event.occurred_on < subscription.period_end
        ]
        for event in billable_events:
            if event.occurred_on < subscription.period_start:
                segment_units[0] += event.units
                continue
            for index, (start, end) in enumerate(zip(segment_starts, segment_ends)):
                if start <= event.occurred_on < end:
                    segment_units[index] += event.units
                    break

        segments = [
            (
                self._store.get_plan(plan_id),
                (end - start).days,
                units,
            )
            for plan_id, start, end, units in zip(
                segment_plan_ids, segment_starts, segment_ends, segment_units
            )
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            self._store.get_plan(subscription.plan_id),
            customer,
            discount,
            segments=segments,
        )

        for event in billable_events:
            event.billed = True

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        if subscription.plan_changes:
            subscription.plan_id = subscription.plan_changes[-1].plan_id
            subscription.plan_changes.clear()
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
