from datetime import date

from ledger.invoices import PlanSegment, build_invoice
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
        return self._store.record_usage(
            subscription_id, UsageEvent(event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve the plan before changing any state so an unknown plan is
        # rejected atomically.
        self._store.get_plan(new_plan_id)
        changes = self._store.get_plan_changes(subscription_id)
        latest_change = changes[-1] if changes else None
        current_plan_id = latest_change.plan_id if latest_change else subscription.plan_id

        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or (latest_change is not None and effective_on <= latest_change.effective_on)
        ):
            raise ValueError("effective_on must be within the open period and increasing")
        if new_plan_id == current_plan_id:
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
        segments: list[PlanSegment] = []
        segment_start = subscription.period_start
        current_plan = plan
        for change in changes:
            segments.append(PlanSegment(current_plan, segment_start, change.effective_on))
            segment_start = change.effective_on
            current_plan = self._store.get_plan(change.plan_id)
        segments.append(PlanSegment(current_plan, segment_start, subscription.period_end))

        eligible_events = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            segments=segments,
            usage_events=eligible_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.plan_id = current_plan.plan_id
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.consume_usage_events(
            subscription_id, {event.event_id for event in eligible_events}
        )
        self._store.clear_plan_changes(subscription_id)

        self._store.save_invoice(invoice)
        return invoice
