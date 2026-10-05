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
        # Keyed by subscription id, then by event id.
        self._usage_events: dict[str, dict[str, UsageEvent]] = {}
        # Plan changes in the open period, keyed by subscription id.
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

    def get_usage_event(self, subscription_id: str, event_id: str) -> UsageEvent | None:
        return self._usage_events.get(subscription_id, {}).get(event_id)

    def add_usage_event(self, event: UsageEvent) -> None:
        self._usage_events.setdefault(event.subscription_id, {})[event.event_id] = event

    def list_usage_events(self, subscription_id: str) -> list[UsageEvent]:
        return list(self._usage_events.get(subscription_id, {}).values())

    def add_plan_change(self, change: PlanChange) -> None:
        self._plan_changes.setdefault(change.subscription_id, []).append(change)

    def list_plan_changes(self, subscription_id: str) -> list[PlanChange]:
        return list(self._plan_changes.get(subscription_id, []))

    def clear_plan_changes(self, subscription_id: str) -> None:
        self._plan_changes.pop(subscription_id, None)
