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
        self._usage_events: dict[str, dict[str, UsageEvent]] = {}
        self._usage_event_ids: dict[str, set[str]] = {}
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
        self._usage_event_ids.setdefault(subscription.subscription_id, set())
        self._plan_changes.setdefault(subscription.subscription_id, [])

    def get_subscription(self, subscription_id: str) -> Subscription:
        return self._subscriptions[subscription_id]

    def add_usage_event(self, subscription_id: str, event: UsageEvent) -> bool:
        events = self._usage_events[subscription_id]
        event_ids = self._usage_event_ids[subscription_id]
        if event.event_id in event_ids:
            return False
        events[event.event_id] = event
        event_ids.add(event.event_id)
        return True

    def get_usage_events(self, subscription_id: str) -> list[UsageEvent]:
        return list(self._usage_events[subscription_id].values())

    def remove_usage_events(self, subscription_id: str, event_ids: set[str]) -> None:
        events = self._usage_events[subscription_id]
        for event_id in event_ids:
            del events[event_id]

    def get_plan_changes(self, subscription_id: str) -> list[PlanChange]:
        return list(self._plan_changes[subscription_id])

    def add_plan_change(self, subscription_id: str, change: PlanChange) -> None:
        self._plan_changes[subscription_id].append(change)

    def clear_plan_changes(self, subscription_id: str) -> None:
        self._plan_changes[subscription_id].clear()

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
