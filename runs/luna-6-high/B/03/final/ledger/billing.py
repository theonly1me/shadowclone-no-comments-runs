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
        subscription = self._store.get_subscription(subscription_id)
        if event_id in subscription.usage_events:
            return False
        if units <= 0:
            raise ValueError("units must be greater than zero")
        subscription.usage_events[event_id] = UsageEvent(
            event_id=event_id, units=units, occurred_on=occurred_on
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
        previous_date = (
            previous_change.effective_on
            if previous_change
            else subscription.period_start
        )
        current_plan_id = (
            previous_change.plan_id if previous_change else subscription.plan_id
        )
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > previous_date
        ):
            raise ValueError(
                "effective_on must be strictly within the open period and changes"
            )
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        subscription.plan_changes.append(
            PlanChange(plan_id=new_plan.plan_id, effective_on=effective_on)
        )

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        current_plan = self._store.get_plan(subscription.plan_id)
        segments = []
        segment_start = subscription.period_start
        for change in subscription.plan_changes:
            segments.append(
                (current_plan, segment_start, change.effective_on)
            )
            current_plan = self._store.get_plan(change.plan_id)
            segment_start = change.effective_on
        segments.append((current_plan, segment_start, subscription.period_end))

        segment_units = [0 for _ in segments]
        billable_events = []
        for event in subscription.usage_events.values():
            if event.billed or event.occurred_on >= subscription.period_end:
                continue
            segment_index = 0
            if event.occurred_on >= subscription.period_start:
                for index, (_, _, segment_end) in enumerate(segments):
                    if event.occurred_on < segment_end:
                        segment_index = index
                        break
            segment_units[segment_index] += event.units
            billable_events.append(event)

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            segment_units,
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in billable_events:
            event.billed = True

        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        subscription.plan_id = current_plan.plan_id
        subscription.plan_changes.clear()

        self._store.save_invoice(invoice)
        return invoice
