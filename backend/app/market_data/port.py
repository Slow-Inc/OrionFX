"""The market-data read port (#29). A deterministic replay source implements it now;
the MT5 read adapter (blocked: no terminal, like #17) implements it later."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawTick:
    broker_symbol: str
    broker_time: datetime
    """Naive broker wall time, as MT5 reports it."""
    broker_utc_offset_minutes: int
    bid: Decimal
    ask: Decimal


class MarketDataPort(Protocol):
    def ticks_since(self, cursor: int, *, limit: int = 1000) -> list[tuple[int, RawTick]]:
        """(cursor, tick) pairs after `cursor`, oldest first; cursors increase."""
        ...


class ReplaySource:
    """Replays a fixed tick list; cursor n is the n-th tick (1-based)."""

    def __init__(self, ticks: list[RawTick]) -> None:
        self._ticks = list(ticks)

    def ticks_since(self, cursor: int, *, limit: int = 1000) -> list[tuple[int, RawTick]]:
        start = max(cursor, 0)
        return [(i + 1, t) for i, t in enumerate(self._ticks[start : start + limit], start)]
