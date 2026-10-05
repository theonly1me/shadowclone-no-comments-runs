from datetime import date

from ledger.invoices import BillingSegment, build_invoice
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
            raise ValueError("units must be greater than 0")
        subscription.usage_events[event_id] = UsageEvent(event_id, units, occurred_on)
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
            if previous_change
            else subscription.period_start
        )
        if not (subscription.period_start < effective_on < subscription.period_end):
            raise ValueError("effective_on must be inside the open period")
        if effective_on <= lower_bound:
            raise ValueError("plan changes must be strictly ordered")

        current_plan_id = (
            previous_change.plan_id if previous_change else subscription.plan_id
        )
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        subscription.plan_changes.append(PlanChange(new_plan.plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        period_start = subscription.period_start
        period_end = subscription.period_end
        boundaries = [period_start]
        plan_ids = [subscription.plan_id]
        for change in subscription.plan_changes:
            boundaries.append(change.effective_on)
            plan_ids.append(change.plan_id)
        boundaries.append(period_end)
        segments = [
            BillingSegment(
                boundaries[index],
                boundaries[index + 1],
                self._store.get_plan(plan_id),
            )
            for index, plan_id in enumerate(plan_ids)
        ]
        billed_events = [
            event
            for event in subscription.usage_events.values()
            if event.occurred_on < period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            billed_events,
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # Carry forward the plan at the end of this period and advance by the
        # same length as the period just billed.
        length = period_end - period_start
        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes.clear()
        for event in billed_events:
            del subscription.usage_events[event.event_id]
        subscription.period_start = period_end
        subscription.period_end = period_end + length

        self._store.save_invoice(invoice)
        return invoice
