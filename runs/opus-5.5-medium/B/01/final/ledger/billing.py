from datetime import date

from ledger.invoices import Segment, build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.storage import InMemoryStore


class BillingService:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def record_usage(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self._store.get_subscription(subscription_id)
        if units <= 0:
            raise ValueError("units must be greater than 0")
        if self._store.has_usage_event(subscription_id, event_id):
            return False
        self._store.add_usage_event(
            UsageEvent(subscription_id, event_id, units, occurred_on)
        )
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)
        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must be inside the open period")
        changes = subscription.plan_changes
        if changes and effective_on <= changes[-1].effective_on:
            raise ValueError("effective_on must be after the previous change")
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on this plan")
        changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        bounds = [(subscription.plan_id, subscription.period_start)]
        bounds += [(c.plan_id, c.effective_on) for c in subscription.plan_changes]
        ends = [start for _, start in bounds[1:]] + [subscription.period_end]

        events = [
            event
            for event in self._store.list_usage_events(subscription_id)
            if event.invoice_id is None and event.occurred_on < subscription.period_end
        ]

        segments = []
        for index, ((plan_id, start), end) in enumerate(zip(bounds, ends)):
            units = sum(
                event.units
                for event in events
                if start <= event.occurred_on < end
                or (index == 0 and event.occurred_on < start)
            )
            segments.append(
                Segment(self._store.get_plan(plan_id), (end - start).days, units)
            )

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in events:
            event.invoice_id = invoice.invoice_id

        subscription.plan_id = bounds[-1][0]
        subscription.plan_changes = []

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
