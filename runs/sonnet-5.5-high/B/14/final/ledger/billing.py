from datetime import date

from ledger.invoices import build_invoice
from ledger.models import Invoice, PlanChange, UsageEvent
from ledger.segments import build_segments
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
        return self._store.add_usage_event(
            UsageEvent(event_id, subscription_id, units, occurred_on)
        )

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        self._store.get_plan(new_plan_id)

        if subscription.plan_changes:
            current_plan_id = subscription.plan_changes[-1].plan_id
            earliest = subscription.plan_changes[-1].effective_on
        else:
            current_plan_id = subscription.plan_id
            earliest = subscription.period_start

        if not earliest < effective_on < subscription.period_end:
            raise ValueError("effective_on is outside the allowed range")
        if new_plan_id == current_plan_id:
            raise ValueError("plan is already current")

        subscription.plan_changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None
        segments = build_segments(subscription, self._store)
        usage = [
            event
            for event in self._store.get_unbilled_usage(subscription_id)
            if event.occurred_on < subscription.period_end
        ]

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            segments,
            usage,
            customer,
            discount,
        )

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        for event in usage:
            event.billed = True

        length = subscription.period_end - subscription.period_start
        subscription.plan_id = segments[-1].plan.plan_id
        subscription.plan_changes = []
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length

        self._store.save_invoice(invoice)
        return invoice
