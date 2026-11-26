"""Candles and data quality from ticks (#29; DAT-01, DAT-04, UST-03).

- Ticks are normalized to UTC and mapped to the canonical symbol; unmapped
  symbols, crossed quotes, duplicates and out-of-order ticks are dropped and
  counted, never folded into a bar.
- M5, M15 and H1 bars are built from the same tick stream on UTC boundaries, so
  every higher bar equals the aggregate of its M5 bars.
- A bar is final only when a tick from a later interval arrives; `available_at`
  is that arrival time, never earlier than the close (no look-ahead).
- Missing intervals are not filled with synthetic bars: the next bar carries a
  GAP flag. Tick count is broker tick volume, not traded volume (CAT-T09-01).
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.core.contracts import CandleClosed, MarketSnapshot
from app.core.enums import DataQuality, Timeframe
from app.market_data.port import RawTick
from app.market_data.symbols import SymbolMap
from app.market_data.time import to_utc

FRAMES = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
}
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
QUOTE_STALE_AFTER = timedelta(seconds=10)  # [PROPOSED]; M06 applies its own policy age


class Issue(StrEnum):
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    INVALID_QUOTE = "INVALID_QUOTE"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    GAP = "GAP"


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str
    time: datetime
    bid: Decimal
    ask: Decimal
    offset_minutes: int


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_count: int
    max_spread: Decimal
    available_at: datetime
    flags: tuple[Issue, ...] = ()

    def as_event(self) -> CandleClosed:
        return CandleClosed(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=self.open_time,
            close_time=self.close_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=Decimal(self.tick_count),  # broker tick volume, labelled as such
            spread=self.max_spread,
            available_at=self.available_at,
        )


@dataclass(slots=True)
class _Open:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    ticks: int
    max_spread: Decimal


def _floor(t: datetime, size: timedelta) -> datetime:
    return EPOCH + ((t - EPOCH) // size) * size


@dataclass
class CandleEngine:
    symbols: SymbolMap
    issues: Counter[Issue] = field(default_factory=Counter)
    _open: dict[Timeframe, _Open] = field(default_factory=dict)
    _last_close: dict[Timeframe, datetime] = field(default_factory=dict)
    _last: Tick | None = None

    def _normalize(self, raw: RawTick) -> Tick | None:
        symbol = self.symbols.canonical(raw.broker_symbol)
        if symbol is None:
            self.issues[Issue.SYMBOL_MISMATCH] += 1
            return None
        if raw.ask < raw.bid or raw.bid <= 0:
            self.issues[Issue.INVALID_QUOTE] += 1
            return None
        tick = Tick(
            symbol,
            to_utc(raw.broker_time, raw.broker_utc_offset_minutes),
            raw.bid,
            raw.ask,
            raw.broker_utc_offset_minutes,
        )
        last = self._last
        if last is not None:
            if tick.time < last.time:
                self.issues[Issue.OUT_OF_ORDER] += 1
                return None
            if (tick.time, tick.bid, tick.ask) == (last.time, last.bid, last.ask):
                self.issues[Issue.DUPLICATE] += 1
                return None
        return tick

    def on_tick(self, raw: RawTick) -> list[Bar]:
        """Feed one tick; returns the bars this tick made final (all timeframes)."""
        tick = self._normalize(raw)
        if tick is None:
            return []
        closed: list[Bar] = []
        for tf, size in FRAMES.items():
            cur = self._open.get(tf)
            if cur is not None and tick.time >= cur.open_time + size:
                closed.append(self._close(tick, tf, size, cur))
                cur = None
            spread = tick.ask - tick.bid
            if cur is None:
                self._open[tf] = _Open(
                    _floor(tick.time, size), tick.bid, tick.bid, tick.bid, tick.bid, 1, spread
                )
            else:
                cur.high = max(cur.high, tick.bid)
                cur.low = min(cur.low, tick.bid)
                cur.close = tick.bid
                cur.ticks += 1
                cur.max_spread = max(cur.max_spread, spread)
        self._last = tick
        return closed

    def _close(self, tick: Tick, tf: Timeframe, size: timedelta, cur: _Open) -> Bar:
        previous = self._last_close.get(tf)
        gap = previous is not None and previous < cur.open_time
        bar = Bar(
            symbol=tick.symbol,
            timeframe=tf,
            open_time=cur.open_time,
            close_time=cur.open_time + size,
            open=cur.open,
            high=cur.high,
            low=cur.low,
            close=cur.close,
            tick_count=cur.ticks,
            max_spread=cur.max_spread,
            available_at=tick.time,
            flags=(Issue.GAP,) if gap else (),
        )
        if gap:
            self.issues[Issue.GAP] += 1
        self._last_close[tf] = bar.close_time
        return bar

    def snapshot(self, now: datetime, symbol: str = "XAUUSD") -> MarketSnapshot:
        last = self._last
        if last is None:
            return MarketSnapshot(
                symbol=symbol,
                bid=None,
                ask=None,
                spread=None,
                tick_time=None,
                quality=DataQuality.MISSING,
            )
        stale = now - last.time > QUOTE_STALE_AFTER
        return MarketSnapshot(
            symbol=last.symbol,
            bid=last.bid,
            ask=last.ask,
            spread=last.ask - last.bid,
            tick_time=last.time,
            broker_time_offset_minutes=last.offset_minutes,
            quality=DataQuality.STALE if stale else DataQuality.OK,
        )
