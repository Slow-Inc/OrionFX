"""When to fetch news (#38; LLM-02: "event/adaptive schedule; not every tick").

A pure decision from (now, last fetch, calendar, policy): fetch on a base
interval, and on a tighter interval inside a window around each high-impact
calendar event. A clock that moved backwards (NTP step, a last fetch restored
from another host) fetches at once rather than going quiet. All intervals are
[PROPOSED].
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.contracts import ContractModel
from app.core.enums import EventImpact
from app.core.time import require_utc
from app.core.types import UtcDatetime


class CalendarEvent(ContractModel):
    name: str
    at: UtcDatetime
    impact: EventImpact


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    base_interval: timedelta = timedelta(minutes=30)
    event_window: timedelta = timedelta(minutes=15)
    event_interval: timedelta = timedelta(minutes=2)


def _near_event(now: datetime, calendar: Sequence[CalendarEvent], window: timedelta) -> bool:
    return any(e.impact is EventImpact.HIGH and abs(now - e.at) <= window for e in calendar)


def should_fetch(
    now: datetime,
    *,
    last_fetch: datetime | None,
    calendar: Sequence[CalendarEvent],
    policy: FetchPolicy,
) -> bool:
    require_utc(now)
    if last_fetch is None or now < require_utc(last_fetch):
        return True
    near = _near_event(now, calendar, policy.event_window)
    return now - last_fetch >= (policy.event_interval if near else policy.base_interval)
