"""UTC-only time helpers. Broker server time travels as separate metadata (M02)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def require_utc(value: datetime) -> datetime:
    """Pydantic validator: the value must already be expressed in UTC (offset 0)."""
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC (offset +00:00)")
    return value
