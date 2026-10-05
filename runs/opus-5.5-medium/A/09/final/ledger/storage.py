from ledger.models import (
    Customer,
    DiscountCode,
    Invoice,
    Plan,
    PlanChange,
    Subscription,
    UsageEvent,
)


class InMemoryStore:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._customers: dict[str, Customer] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._discount_codes: dict[str, DiscountCode] = {}
        self._invoices: dict[str, Invoice] = {}
        # Usage events by subscription, keyed by event id. Billed ids are kept
        # so a replayed event stays a duplicate after it has been invoiced.
        self._usage_events: dict[str, dict[str, UsageEvent]] = {}
        self._billed_event_ids: dict[str, set[str]] = {}
        self._plan_changes: dict[str, list[PlanChange]] = {}

    def add_plan(self, plan: Plan) -> None:
        self._plans[plan.plan_id] = plan

    def get_plan(self, plan_id: str) -> Plan:
        return self._plans[plan_id]

    def add_customer(self, customer: Customer) -> None:
        self._customers[customer.customer_id] = customer

    def get_customer(self, customer_id: str) -> Customer:
        return self._customers[customer_id]

    def add_subscription(self, subscription: Subscription) -> None:
        self._subscriptions[subscription.subscription_id] = subscription

    def get_subscription(self, subscription_id: str) -> Subscription:
        return self._subscriptions[subscription_id]

    def add_discount_code(self, code: DiscountCode) -> None:
        self._discount_codes[code.code] = code

    def get_discount_code(self, code: str) -> DiscountCode:
        return self._discount_codes[code]

    def next_invoice_id(self) -> str:
        return f"inv-{len(self._invoices) + 1}"

    def save_invoice(self, invoice: Invoice) -> None:
        self._invoices[invoice.invoice_id] = invoice

    def get_invoice(self, invoice_id: str) -> Invoice:
        return self._invoices[invoice_id]

    def has_usage_event(self, subscription_id: str, event_id: str) -> bool:
        return event_id in self._usage_events.get(subscription_id, {})

    def add_usage_event(self, subscription_id: str, event: UsageEvent) -> None:
        self._usage_events.setdefault(subscription_id, {})[event.event_id] = event

    def unbilled_usage_events(self, subscription_id: str) -> list[UsageEvent]:
        billed = self._billed_event_ids.get(subscription_id, set())
        return [
            event
            for event in self._usage_events.get(subscription_id, {}).values()
            if event.event_id not in billed
        ]

    def mark_usage_billed(self, subscription_id: str, event_ids: list[str]) -> None:
        self._billed_event_ids.setdefault(subscription_id, set()).update(event_ids)

    def get_plan_changes(self, subscription_id: str) -> list[PlanChange]:
        return list(self._plan_changes.get(subscription_id, []))

    def add_plan_change(self, subscription_id: str, change: PlanChange) -> None:
        self._plan_changes.setdefault(subscription_id, []).append(change)

    def clear_plan_changes(self, subscription_id: str) -> None:
        self._plan_changes.pop(subscription_id, None)
