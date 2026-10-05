from dataclasses import dataclass
from datetime import date

from ledger.models import Plan, Subscription
from ledger.storage import InMemoryStore


@dataclass(frozen=True)
class Segment:
    plan: Plan
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def build_segments(subscription: Subscription, store: InMemoryStore) -> list[Segment]:
    segments = []
    plan_id = subscription.plan_id
    start = subscription.period_start
    for change in subscription.plan_changes:
        segments.append(Segment(store.get_plan(plan_id), start, change.effective_on))
        plan_id = change.plan_id
        start = change.effective_on
    segments.append(Segment(store.get_plan(plan_id), start, subscription.period_end))
    return segments


def segment_index_for(segments: list[Segment], occurred_on: date) -> int:
    index = 0
    for i, segment in enumerate(segments):
        if segment.start <= occurred_on:
            index = i
    return index
