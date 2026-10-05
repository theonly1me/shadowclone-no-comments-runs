from datetime import date

from ledger.models import Customer, DiscountCode, Invoice, Plan, Subscription, UsageEvent


class InMemoryStore:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._customers: dict[str, Customer] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._discount_codes: dict[str, DiscountCode] = {}
        self._invoices: dict[str, Invoice] = {}
        self._usage_events: dict[str, dict[str, UsageEvent]] = {}
        self._seen_usage_event_ids: dict[str, set[str]] = {}

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

    def record_usage_event(
        self, subscription_id: str, event_id: str, units: int, occurred_on: date
    ) -> bool:
        self.get_subscription(subscription_id)
        events = self._usage_events.setdefault(subscription_id, {})
        seen_ids = self._seen_usage_event_ids.setdefault(subscription_id, set())
        if event_id in seen_ids:
            return False
        seen_ids.add(event_id)
        events[event_id] = UsageEvent(event_id, units, occurred_on)
        return True

    def has_usage_event(self, subscription_id: str, event_id: str) -> bool:
        self.get_subscription(subscription_id)
        return event_id in self._seen_usage_event_ids.get(subscription_id, set())

    def get_usage_events(self, subscription_id: str) -> list[UsageEvent]:
        self.get_subscription(subscription_id)
        return list(self._usage_events.get(subscription_id, {}).values())

    def remove_usage_events(self, subscription_id: str, event_ids: set[str]) -> None:
        events = self._usage_events.get(subscription_id, {})
        for event_id in event_ids:
            events.pop(event_id, None)

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
