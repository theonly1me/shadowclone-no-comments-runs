from ledger.models import Customer, DiscountCode, Invoice, Plan, Subscription, UsageEvent


class InMemoryStore:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._customers: dict[str, Customer] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._discount_codes: dict[str, DiscountCode] = {}
        self._invoices: dict[str, Invoice] = {}
        self._usage_events: dict[str, dict[str, UsageEvent]] = {}
        self._billed_usage_events: set[tuple[str, str]] = set()

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

    def add_usage_event(self, subscription_id: str, event: UsageEvent) -> None:
        self._usage_events.setdefault(subscription_id, {})[event.event_id] = event

    def has_usage_event(self, subscription_id: str, event_id: str) -> bool:
        return event_id in self._usage_events.get(subscription_id, {})

    def get_unbilled_usage_events(self, subscription_id: str) -> list[UsageEvent]:
        return [
            event
            for event in self._usage_events.get(subscription_id, {}).values()
            if (subscription_id, event.event_id) not in self._billed_usage_events
        ]

    def mark_usage_events_billed(
        self, subscription_id: str, event_ids: set[str]
    ) -> None:
        self._billed_usage_events.update(
            (subscription_id, event_id) for event_id in event_ids
        )

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
