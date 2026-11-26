"""Broker server time -> UTC (#29; master plan M02: UTC normalization with broker-time
metadata, DST tests).

MT5 reports naive broker wall time; the source supplies the broker's UTC offset
in force for each record (it changes at DST). UTC is the only time stored.
"""

from datetime import UTC, datetime, timedelta


def to_utc(broker_time: datetime, offset_minutes: int) -> datetime:
    if broker_time.tzinfo is not None:
        raise ValueError("broker time must be naive wall time; its offset is passed separately")
    return (broker_time - timedelta(minutes=offset_minutes)).replace(tzinfo=UTC)
