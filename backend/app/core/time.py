"""UTC-only time helpers. Broker server time travels as separate metadata (M02)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.enums import Timeframe

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def utc_iso(t: datetime) -> str:
    """UTC ISO text, which sorts in time order: store and query times in this form."""
    if t.tzinfo is None:
        raise ValueError("naive datetime: pass an aware UTC time")
    return t.astimezone(UTC).isoformat()


def require_utc(value: datetime) -> datetime:
    """Pydantic validator: the value must already be expressed in UTC (offset 0)."""
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC (offset +00:00)")
    return value


CLOCK_SKEW = timedelta(seconds=2)
"""Tolerance for a timestamp slightly ahead of our clock; beyond it, data is from the future."""


def broker_to_utc(broker_time: datetime, offset_minutes: int) -> datetime:
    """MT5 reports naive broker wall time; the offset in force (it changes at DST) is
    passed separately. UTC is the only time stored."""
    if broker_time.tzinfo is not None:
        raise ValueError("broker time must be naive wall time; its offset is passed separately")
    return (broker_time - timedelta(minutes=offset_minutes)).replace(tzinfo=UTC)


def fresh(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
    """Not older than `max_age`, and not from the future beyond the clock skew."""
    return -CLOCK_SKEW <= now - observed_at <= max_age


TIMEFRAME_DURATION = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
}


def live(start: datetime, end: datetime, now: datetime) -> bool:
    """Known at `now` (no look-ahead) and not yet expired."""
    return start <= now < end
