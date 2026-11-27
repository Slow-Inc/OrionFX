"""Trading-session schedules (#124, PRD #120 slice 0a; DAT-01, DAT-04).

A schedule says when a market is closed on purpose: the daily break and the weekend.
Bars are never expected inside a closed period, so a technique window that spans one is
still contiguous and the first bar after it is not a gap. A hole while the market is
open is still a gap: the schedule only excuses closures it names.

The schedule is defined in the exchange's local time zone, so daylight saving moves it
in UTC by itself. It is a specification per data source and is never inferred from the
bars. `audit` checks a schedule against stored bars instead: bars inside a closed period
mean the schedule is wrong for that source, and missing open slots are real gaps.

`XAUUSD_NY_1700` is the common MT5 gold schedule: closed daily 17:00-18:00 New York time,
and from Friday 17:00 to Sunday 18:00. It is [PROPOSED]: confirm it against the broker's
symbol specification and the data (`audit`) before relying on it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.contracts import CandleClosed

__all__ = ["XAUUSD_NY_1700", "ScheduleAudit", "SessionSchedule", "audit"]

STEP = timedelta(minutes=5)
"""Closures start and end on 5-minute boundaries, the finest bar this system builds."""
MAX_CLOSED = timedelta(days=4)
"""No scheduled closure is longer than this; a longer search means a broken schedule."""
FRIDAY, SATURDAY, SUNDAY = 4, 5, 6


@dataclass(frozen=True, slots=True)
class SessionSchedule:
    schedule_id: str
    tz: str
    daily_close: time
    """Local time the daily break starts (Monday to Thursday) and the week ends (Friday)."""
    daily_reopen: time
    """Local time the market reopens after the daily break, and on Sunday."""

    def __post_init__(self) -> None:
        ZoneInfo(self.tz)  # an unknown zone fails here, not on the first bar
        if not self.daily_close < self.daily_reopen:
            raise ValueError("daily_close must be before daily_reopen on the same day")
        for t in (self.daily_close, self.daily_reopen):
            if t.minute % 5 or t.second or t.microsecond:
                raise ValueError("closures must start and end on 5-minute boundaries")

    def is_open(self, t: datetime) -> bool:
        local = t.astimezone(ZoneInfo(self.tz))
        day, clock = local.weekday(), local.time()
        if day == SATURDAY:
            return False
        if day == FRIDAY:
            return clock < self.daily_close
        if day == SUNDAY:
            return clock >= self.daily_reopen
        return not self.daily_close <= clock < self.daily_reopen

    def expects(self, open_time: datetime, size: timedelta) -> bool:
        """Whether a bar opening at `open_time` should exist: the market is open for
        some part of it."""
        steps = max(1, size // STEP)
        return any(self.is_open(open_time + k * STEP) for k in range(steps))

    def next_open_slot(self, open_time: datetime, size: timedelta) -> datetime:
        """The open time of the first bar expected after the bar opening at `open_time`."""
        return _next_open_slot(self, open_time, size)

    def _search(self, open_time: datetime, size: timedelta) -> datetime:
        t = open_time + size
        limit = open_time + MAX_CLOSED
        while not self.expects(t, size):
            t += size
            if t > limit:
                raise ValueError(f"{self.schedule_id}: no open slot within {MAX_CLOSED}")
        return t


@lru_cache(maxsize=65536)
def _next_open_slot(schedule: SessionSchedule, open_time: datetime, size: timedelta) -> datetime:
    """Memoised: every technique asks the same question for every pair in its window."""
    return schedule._search(open_time, size)


XAUUSD_NY_1700 = SessionSchedule(
    schedule_id="xauusd-ny-1700",
    tz="America/New_York",
    daily_close=time(17, 0),
    daily_reopen=time(18, 0),
)
"""[PROPOSED] See the module docstring."""


def next_slot(schedule: SessionSchedule | None, open_time: datetime, size: timedelta) -> datetime:
    """The next expected bar's open time; with no schedule, the market never closes."""
    return open_time + size if schedule is None else schedule.next_open_slot(open_time, size)


@dataclass(frozen=True, slots=True)
class ScheduleAudit:
    bars: int
    in_closed: int
    """Bars inside a closed period: the schedule does not fit this source."""
    unexpected_gaps: int
    """Expected slots with no bar between consecutive bars: real missing data."""

    @property
    def fits(self) -> bool:
        return self.in_closed == 0


def audit(schedule: SessionSchedule, bars: Sequence[CandleClosed]) -> ScheduleAudit:
    """Check a schedule against bars of one symbol and timeframe, in time order."""
    ordered = sorted(bars, key=lambda b: b.open_time)
    in_closed = gaps = 0
    for i, b in enumerate(ordered):
        size = b.close_time - b.open_time
        if not schedule.expects(b.open_time, size):
            in_closed += 1
        if i:
            t = schedule.next_open_slot(ordered[i - 1].open_time, size)
            while t < b.open_time:
                gaps += 1
                t = schedule.next_open_slot(t, size)
    return ScheduleAudit(len(ordered), in_closed, gaps)
