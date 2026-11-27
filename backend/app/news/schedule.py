"""When to fetch news (#38; LLM-02: "event/adaptive schedule; not every tick").

A pure decision from (now, last fetch, calendar, policy): fetch on a base
interval, and on a tighter interval inside a window around each high-impact
calendar event. All intervals are [PROPOSED].
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    name: str
    at: datetime
    impact: Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    base_interval: timedelta = timedelta(minutes=30)
    event_window: timedelta = timedelta(minutes=15)
    event_interval: timedelta = timedelta(minutes=2)


def _near_event(now: datetime, calendar: Sequence[CalendarEvent], window: timedelta) -> bool:
    return any(e.impact == "high" and abs(now - e.at) <= window for e in calendar)


def should_fetch(
    now: datetime,
    *,
    last_fetch: datetime | None,
    calendar: Sequence[CalendarEvent],
    policy: FetchPolicy,
) -> bool:
    if last_fetch is None:
        return True
    interval = (
        policy.event_interval
        if _near_event(now, calendar, policy.event_window)
        else policy.base_interval
    )
    return now - last_fetch >= interval
