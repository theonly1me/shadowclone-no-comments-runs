from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        """Record a usage event. Returns False if the event id was already recorded."""
        self._store.get_subscription(subscription_id)
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.add_usage_event(
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        previous_change = (
            subscription.plan_changes[-1][0] if subscription.plan_changes else None
        )
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        if previous_change is not None and effective_on <= previous_change:
            raise ValueError("effective_on must be after the previous plan change")
        current_plan_id = (
            subscription.plan_changes[-1][1]
            if subscription.plan_changes
            else subscription.plan_id
        )
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on that plan")

        subscription.plan_changes.append((effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        # Plan changes split the period into segments.
        segments = []
        segment_start, segment_plan = subscription.period_start, plan
        for effective_on, plan_id in subscription.plan_changes:
            segments.append(Segment(segment_plan, segment_start, effective_on))
            segment_start, segment_plan = effective_on, self._store.get_plan(plan_id)
        segments.append(Segment(segment_plan, segment_start, subscription.period_end))

        usage_events = [
            event
            for event in self._store.get_unbilled_usage(subscription_id)
            if event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            segments,
            usage_events,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage_events:
            event.billed = True

        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes = []

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
