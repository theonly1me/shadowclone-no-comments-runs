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
        self._store.get_subscription(subscription_id)  # KeyError if unknown
        if units <= 0:
            raise ValueError("units must be greater than 0")
        return self._store.add_usage_event(
            UsageEvent(event_id, subscription_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)  # KeyError if unknown

        if not subscription.period_start < effective_on < subscription.period_end:
            raise ValueError("effective_on must fall inside the open period")
        if subscription.plan_changes:
            last_date, current_plan_id = subscription.plan_changes[-1]
            if effective_on <= last_date:
                raise ValueError("effective_on must be after the previous change")
        else:
            current_plan_id = subscription.plan_id
        if new_plan_id == current_plan_id:
            raise ValueError("subscription is already on that plan")

        subscription.plan_changes.append((effective_on, new_plan_id))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        # Segment boundaries: the period start plus each plan change.
        starts = [subscription.period_start] + [d for d, _ in subscription.plan_changes]
        plan_ids = [subscription.plan_id] + [p for _, p in subscription.plan_changes]
        ends = starts[1:] + [subscription.period_end]
        units = [0] * len(starts)

        # Unbilled events up to the end of the open period; late events join
        # the first segment, the rest the segment containing their date.
        events = [
            e
            for e in self._store.get_usage_events(subscription_id)
            if not e.billed and e.occurred_on < subscription.period_end
        ]
        for event in events:
            index = max(
                (i for i, start in enumerate(starts) if start <= event.occurred_on),
                default=0,
            )
            units[index] += event.units

        segments = [
            Segment(self._store.get_plan(plan_id), (end - start).days, seg_units)
            for plan_id, start, end, seg_units in zip(plan_ids, starts, ends, units)
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(), subscription, segments, customer, discount
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent
        for event in events:
            event.billed = True

        subscription.plan_id = plan_ids[-1]
        subscription.plan_changes = []
        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
