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
        self._store.get_subscription(subscription_id)
        events = self._store.get_usage_events(subscription_id)
        if event_id in events:
            return False
        events[event_id] = UsageEvent(event_id, units, occurred_on)
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve the plan before mutating any period state.
        self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        previous_change = changes[-1] if changes else None
        lower_bound = (
            previous_change.effective_on if previous_change else subscription.period_start
        )
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > lower_bound
        ):
            raise ValueError("effective_on must be strictly increasing within the period")
        current_plan_id = previous_change.plan_id if previous_change else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        changes.append(PlanChange(effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        changes = self._store.get_plan_changes(subscription_id)
        events = self._store.get_usage_events(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        boundaries = [subscription.period_start]
        boundaries.extend(change.effective_on for change in changes)
        boundaries.append(subscription.period_end)
        plan_ids = [subscription.plan_id]
        plan_ids.extend(change.plan_id for change in changes)
        segments = []
        for index, (segment_start, segment_end) in enumerate(
            zip(boundaries, boundaries[1:])
        ):
            segment_plan = self._store.get_plan(plan_ids[index])
            segment_units = sum(
                event.units
                for event in events.values()
                if event.occurred_on < subscription.period_end
                and (
                    (
                        event.occurred_on < subscription.period_start
                        or segment_start <= event.occurred_on
                    )
                    if index == 0
                    else segment_start <= event.occurred_on
                )
                and (
                    index == len(boundaries) - 2
                    or event.occurred_on < segment_end
                )
            )
            segments.append(
                (segment_plan, (segment_end - segment_start).days, segment_units)
            )
        plan = self._store.get_plan(plan_ids[-1])

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
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
        billed_event_ids = [
            event_id
            for event_id, event in events.items()
            if event.occurred_on < subscription.period_end
        ]
        for event_id in billed_event_ids:
            del events[event_id]
        subscription.plan_id = plan.plan_id
        changes.clear()
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
