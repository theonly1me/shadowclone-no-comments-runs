from ledger.models import (
    Customer, DiscountCode, Invoice, Plan, PlanChange, Subscription, UsageEvent,
)


class InMemoryStore:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._customers: dict[str, Customer] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._discount_codes: dict[str, DiscountCode] = {}
        self._invoices: dict[str, Invoice] = {}
        self._usage_events: dict[str, dict[str, UsageEvent]] = {}
        self._billed_usage_ids: dict[str, set[str]] = {}
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
        self._usage_events.setdefault(subscription.subscription_id, {})
        self._billed_usage_ids.setdefault(subscription.subscription_id, set())
        self._plan_changes.setdefault(subscription.subscription_id, [])

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

    def record_usage(self, subscription_id: str, event: UsageEvent) -> bool:
        events = self._usage_events[subscription_id]
        if event.event_id in events:
            return False
        if event.units <= 0:
            raise ValueError("units must be greater than zero")
        events[event.event_id] = event
        return True

    def get_usage_events(self, subscription_id: str) -> list[UsageEvent]:
        billed = self._billed_usage_ids[subscription_id]
        return [
            event
            for event_id, event in self._usage_events[subscription_id].items()
            if event_id not in billed
        ]

    def consume_usage_events(self, subscription_id: str, event_ids: set[str]) -> None:
        self._billed_usage_ids[subscription_id].update(event_ids)

    def get_plan_changes(self, subscription_id: str) -> list[PlanChange]:
        return self._plan_changes[subscription_id]

    def add_plan_change(self, subscription_id: str, change: PlanChange) -> None:
        self._plan_changes[subscription_id].append(change)

    def clear_plan_changes(self, subscription_id: str) -> None:
        self._plan_changes[subscription_id].clear()
