"""The market-data read port (#29). A deterministic replay source implements it now;
the MT5 read adapter (blocked: no terminal, like #17) implements it later. Historical bars
come through `HistoricalBarSource` instead (#128)."""

from collections.abc import Iterator
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


@dataclass(frozen=True, slots=True)
class M1Bar:
    """One closed one-minute bar from a historical source (#128). Prices are bid, as live
    bars are built from bid ticks, except for a futures source (FirstRate GC), whose prices
    are trades. M1 is an import format only: the store holds M5/M15/H1."""

    open_time: datetime
    """Aware; the importer converts it to UTC."""
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    """Volume as the vendor counts it: tick volume for a spot FX vendor, contracts traded
    for a futures vendor (FirstRate). Its meaning is per data source."""
    spread: Decimal | None
    """Ask minus bid in price units (the minute's widest, if the vendor has it)."""
    filled: bool = False
    """Set by the importer only: a flat bar for a minute with no trade (#128)."""


class HistoricalBarSource(Protocol):
    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        """The bars with `start <= open_time < end`, in the order the source holds them
        (the importer checks the order; it does not trust it)."""
        ...
