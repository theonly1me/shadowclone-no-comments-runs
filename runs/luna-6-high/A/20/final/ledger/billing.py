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
        if any(event.event_id == event_id for event in subscription.usage_events):
            return False
        if units <= 0:
            raise ValueError("units must be greater than zero")
        subscription.usage_events.append(UsageEvent(event_id, units, occurred_on))
        return True

    def change_plan(
        self, subscription_id: str, new_plan_id: str, effective_on: date
    ) -> None:
        subscription = self._store.get_subscription(subscription_id)
        # Resolve first so an unknown plan consistently raises KeyError without
        # changing subscription state.
        self._store.get_plan(new_plan_id)
        changes = subscription.plan_changes
        lower_bound = changes[-1].effective_on if changes else subscription.period_start
        current_plan_id = changes[-1].plan_id if changes else subscription.plan_id
        if not (
            subscription.period_start < effective_on < subscription.period_end
            and effective_on > lower_bound
        ):
            raise ValueError("effective_on must be strictly within the open period")
        if new_plan_id == current_plan_id:
            raise ValueError("new plan is already current")
        changes.append(PlanChange(new_plan_id, effective_on))

    def generate_invoice(
        self, subscription_id: str, discount_code: str | None = None
    ) -> Invoice:
        subscription = self._store.get_subscription(subscription_id)
        plan = self._store.get_plan(subscription.plan_id)
        customer = self._store.get_customer(subscription.customer_id)
        discount = self._store.get_discount_code(discount_code) if discount_code else None

        invoice = build_invoice(
            self._store.next_invoice_id(),
            subscription,
            plan,
            customer,
            discount,
            plans={
                change.plan_id: self._store.get_plan(change.plan_id)
                for change in subscription.plan_changes
            }
            | {plan.plan_id: plan},
        )

        # Events are retained after billing to preserve event_id idempotency.
        for event in subscription.usage_events:
            if not event.billed and event.occurred_on < subscription.period_end:
                event.billed = True

        credit_spent = sum(
            -item.amount_cents for item in invoice.line_items if item.kind == "credit"
        )
        customer.credit_balance_cents -= credit_spent

        # The next period has the same length as the one just billed.
        length = subscription.period_end - subscription.period_start
        subscription.period_start = subscription.period_end
        subscription.period_end = subscription.period_end + length
        if subscription.plan_changes:
            subscription.plan_id = subscription.plan_changes[-1].plan_id
            subscription.plan_changes.clear()

        self._store.save_invoice(invoice)
        return invoice
