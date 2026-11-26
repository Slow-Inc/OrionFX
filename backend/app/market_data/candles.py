"""Candles and data quality from ticks (#29; DAT-01, DAT-04, UST-03).

- One engine per canonical symbol. Ticks are normalized to UTC and mapped
  explicitly (no fuzzy matching); other symbols, non-finite or crossed quotes,
  ticks from the future, duplicates and out-of-order ticks are dropped and
  counted, never folded into a bar.
- M5, M15 and H1 bars are built from the same tick stream on UTC boundaries, so
  every higher bar equals the aggregate of its M5 bars.
- A bar is final only when a tick from a later interval arrives; `available_at`
  is that arrival time, never earlier than the close (no look-ahead).
- Missing M5 intervals are not filled with synthetic bars: every bar that
  contains or directly follows the hole carries a GAP flag, on the published
  `CandleClosed` too. Tick count is broker tick volume, not traded volume.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.core.contracts import CandleClosed, MarketSnapshot
from app.core.enums import DataQuality, QualityFlag, Timeframe
from app.core.time import CLOCK_SKEW, TIMEFRAME_DURATION, Clock, broker_to_utc, fresh, utc_now
from app.execution.broker import Quote
from app.market_data.port import RawTick

FRAMES = TIMEFRAME_DURATION
M5 = FRAMES[Timeframe.M5]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
QUOTE_MAX_AGE = timedelta(seconds=5)  # [PROPOSED]; matches the M06 default policy age


class Issue(StrEnum):
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    INVALID_QUOTE = "INVALID_QUOTE"
    FUTURE_TICK = "FUTURE_TICK"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    GAP = QualityFlag.GAP.value


@dataclass(frozen=True, slots=True)
class ClosedBar:
    event: CandleClosed
    """Validated contract (OHLC range, no look-ahead), quality flags included."""

    @property
    def timeframe(self) -> Timeframe:
        return self.event.timeframe


@dataclass(slots=True)
class _Open:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    ticks: int
    max_spread: Decimal
    gap: bool


def _floor(t: datetime, size: timedelta) -> datetime:
    return EPOCH + ((t - EPOCH) // size) * size


@dataclass
class CandleEngine:
    symbol: str
    symbols: Mapping[str, str]
    """Broker symbol -> canonical symbol; only ticks mapping to `symbol` are used."""
    clock: Clock = utc_now
    issues: Counter[Issue] = field(default_factory=Counter)
    _open: dict[Timeframe, _Open] = field(default_factory=dict)
    _last: Quote | None = None
    _last_offset: int | None = None
    _seen_at_last: set[tuple[Decimal, Decimal]] = field(default_factory=set)

    def _normalize(self, raw: RawTick) -> Quote | None:
        if self.symbols.get(raw.broker_symbol) != self.symbol:
            self.issues[Issue.SYMBOL_MISMATCH] += 1
            return None
        finite = raw.bid.is_finite() and raw.ask.is_finite()
        if not finite or raw.bid <= 0 or raw.ask < raw.bid:
            self.issues[Issue.INVALID_QUOTE] += 1
            return None
        time = broker_to_utc(raw.broker_time, raw.broker_utc_offset_minutes)
        if time > self.clock() + CLOCK_SKEW:
            self.issues[Issue.FUTURE_TICK] += 1  # e.g. a stale DST offset
            return None
        last = self._last
        if last is not None:
            if time < last.time:
                self.issues[Issue.OUT_OF_ORDER] += 1
                return None
            if time == last.time and (raw.bid, raw.ask) in self._seen_at_last:
                self.issues[Issue.DUPLICATE] += 1
                return None
        return Quote(self.symbol, raw.bid, raw.ask, time)

    def on_tick(self, raw: RawTick) -> list[ClosedBar]:
        """Feed one tick; returns the bars this tick made final (all timeframes)."""
        tick = self._normalize(raw)
        if tick is None:
            return []
        last = self._last
        # A whole M5 interval with no tick lies between the last tick and this one.
        hole = last is not None and _floor(tick.time, M5) > _floor(last.time, M5) + M5
        if hole:
            self.issues[Issue.GAP] += 1
        spread = tick.ask - tick.bid
        closed: list[ClosedBar] = []
        for tf, size in FRAMES.items():
            cur = self._open.get(tf)
            if cur is not None and tick.time >= cur.open_time + size:
                closed.append(self._close(tick.time, tf, size, cur))
                cur = None
            if cur is None:
                self._open[tf] = _Open(
                    _floor(tick.time, size), tick.bid, tick.bid, tick.bid, tick.bid, 1, spread, hole
                )
            else:
                cur.high = max(cur.high, tick.bid)
                cur.low = min(cur.low, tick.bid)
                cur.close = tick.bid
                cur.ticks += 1
                cur.max_spread = max(cur.max_spread, spread)
                cur.gap = cur.gap or hole
        if last is None or tick.time != last.time:
            self._seen_at_last = set()
        self._seen_at_last.add((tick.bid, tick.ask))
        self._last, self._last_offset = tick, raw.broker_utc_offset_minutes
        return closed

    def _close(self, at: datetime, tf: Timeframe, size: timedelta, cur: _Open) -> ClosedBar:
        return ClosedBar(
            CandleClosed(
                symbol=self.symbol,
                timeframe=tf,
                open_time=cur.open_time,
                close_time=cur.open_time + size,
                open=cur.open,
                high=cur.high,
                low=cur.low,
                close=cur.close,
                volume=Decimal(cur.ticks),  # broker tick volume, labelled as such
                spread=cur.max_spread,
                available_at=at,
                quality_flags=[Issue.GAP.value] if cur.gap else [],
            )
        )

    def snapshot(self, now: datetime) -> MarketSnapshot:
        """Quote issues are counted in `issues`; the snapshot reports freshness."""
        last = self._last
        if last is None:
            return MarketSnapshot(
                symbol=self.symbol,
                bid=None,
                ask=None,
                spread=None,
                tick_time=None,
                quality=DataQuality.MISSING,
            )
        return MarketSnapshot(
            symbol=self.symbol,
            bid=last.bid,
            ask=last.ask,
            spread=last.ask - last.bid,
            tick_time=last.time,
            broker_time_offset_minutes=self._last_offset,
            quality=DataQuality.OK if fresh(last.time, now, QUOTE_MAX_AGE) else DataQuality.STALE,
        )
