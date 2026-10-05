from datetime import date, timedelta

from ledger.invoices import InvoiceSegment, build_usage_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None
        changes = self._store.get_plan_changes(subscription_id)
        segments: list[InvoiceSegment] = []
        segment_start = subscription.period_start
        plan_id = subscription.plan_id
        for change in changes:
            segments.append(
                InvoiceSegment(
                    self._store.get_plan(plan_id), segment_start, change.effective_on, 0
                )
            )
            segment_start = change.effective_on
            plan_id = change.plan_id
        segments.append(
            InvoiceSegment(
                self._store.get_plan(plan_id), segment_start, subscription.period_end, 0
            )
        )
        eligible = [
            event
            for event in self._store.get_usage_events(subscription_id)
            if not self._store.is_usage_event_billed(subscription_id, event.event_id)
            and event.occurred_on < subscription.period_end
        ]
        segment_units = [0] * len(segments)
        for event in eligible:
            index = 0
            if event.occurred_on >= subscription.period_start:
                for candidate, segment in enumerate(segments):
                    if segment.start <= event.occurred_on < segment.end:
                        index = candidate
                        break
            segment_units[index] += event.units
        segments = [
            InvoiceSegment(segment.plan, segment.start, segment.end, segment_units[index])
            for index, segment in enumerate(segments)
        ]
        invoice = build_usage_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        subscription.plan_id = plan_id
        self._store.clear_plan_changes(subscription_id)
        self._store.mark_usage_events_billed(
            subscription_id, {event.event_id for event in eligible}
        )

        self._store.save_invoice(invoice)
        return invoice

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self._store.get_subscription(subscription_id)
        if self._store.has_usage_event(subscription_id, event_id):
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
        if (
            effective_on <= subscription.period_start
            or effective_on >= subscription.period_end
            or effective_on <= previous_date
        ):
            raise ValueError("effective_on must be strictly within the period and increasing")
        if new_plan.plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        self._store.add_plan_change(
            subscription_id, PlanChange(new_plan_id, effective_on)
        )
