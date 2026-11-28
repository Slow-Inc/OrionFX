"""Triple-barrier labels, versioned by `label_version` (#129; research backlog item 0).

A label says how a bet opened at the close of the bar ending at `as_of` resolved, on the
recording-timeframe bars after it. It prices the same event as M05's `expected_value`:
- Barriers sit at +/- K x ATR_M15 from the entry fill, with K_TP = K_SL = TARGET_ATR and
  the ATR read from the recorded snapshot at `as_of`, the value EV reads.
- Prices are bid OHLC. A long fills at ask (bid + spread) and exits at bid; a short fills
  at bid and exits at ask (bid + that bar's spread). Every fill is worse by SLIPPAGE. A
  bar with no spread costs MISSING_SPREAD, never zero. So the spread and the slippage
  sit inside the trigger: a side that resolves at a barrier realises exactly +/- the
  distance, and EV charges only the commission on top (see `fusion.expected_value`).
- A bar touching both barriers counts as SL first (the order inside a bar is unknown).
- The time barrier is N recording bars, N being the forecast horizon's span, counted in
  expected slots of the session schedule (a scheduled closure is not a gap). A side that
  touches neither barrier is FLAT and exits at the N-th bar's close; its payoff lies
  strictly between the barriers.
- A missing bar before both sides resolve gives no label, never a stretched one.

The outcome per side is the direction the market resolved that side's bet in: UP or
DOWN at a barrier (a long's TP is UP, a short's TP is DOWN), FLAT at the time barrier.
A three-class model scores the label of the side it predicts; M05 prices EV from those
three probabilities, with a FLAT's drift taken as 0 (it still pays the fills).

A PROXY, not the real trade: the bot places no per-position SL/TP (RSK-05); its exits
are the Emergency SL, the cycle Profit Target and basket trailing. Only the system
backtest that replays those exits (#131) can say whether a model is usable.

A gap through a barrier is labelled at the barrier: the label says which barrier came
first; it is not an execution simulation. The constants are [PROPOSED]; the ones EV
shares (TARGET_ATR, SLIPPAGE, LABEL_VERSION) live in `app.core.bet`. Changing any of
them changes what every label means, so it needs a new LABEL_VERSION.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.bet import LABEL_VERSION, SLIPPAGE, TARGET_ATR
from app.core.contracts import CandleClosed
from app.core.enums import Direction, Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.learning.bars import BarLog, Horizon, RecordedPrediction
from app.market_data.features import ATR_M15, FEATURE_SCHEMA
from app.market_data.session import SessionSchedule, next_slot
from app.market_data.store import CandleStore

__all__ = [
    "K_SL",
    "K_TP",
    "LABEL_VERSION",
    "MISSING_SPREAD",
    "SLIPPAGE",
    "Outcome",
    "SideLabel",
    "TripleBarrierLabel",
    "label_at",
    "prediction_labels",
    "time_bars",
    "triple_barrier",
]

D = Decimal
K_TP = TARGET_ATR
"""Take-profit distance in ATR_M15. Equal to TARGET_ATR: EV prices this exact bet."""
K_SL = TARGET_ATR
"""Stop-loss distance in ATR_M15. Equal to K_TP: EV prices a symmetric bet."""
MISSING_SPREAD = D("0.80")
"""The spread a bar with none is charged: the default M06 `max_spread`, the widest
spread a trade may open at [PROPOSED; locked by LABEL_VERSION]."""


class Outcome(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


@dataclass(frozen=True, slots=True)
class SideLabel:
    outcome: Outcome
    entry: Decimal
    """The entry fill."""
    exit: Decimal
    """The exit fill: the barrier hit, or the time barrier's close."""
    sign: int
    """+1 for the long, -1 for the short."""

    @property
    def pnl(self) -> Decimal:
        """Realised P&L per unit (USD per oz), net of spread and slippage."""
        return (self.exit - self.entry) * self.sign


@dataclass(frozen=True, slots=True)
class TripleBarrierLabel:
    label_version: str
    distance: Decimal
    """K x ATR_M15: the barrier distance from the entry fill."""
    long: SideLabel
    short: SideLabel
    available_at: datetime
    """When both sides were known: the resolving (or N-th) bar's `available_at`."""
    ends_at: datetime
    """The close of the N-th expected slot: the latest price any label of this bar could
    read, whether or not it resolved earlier (a split purge bound)."""

    def side(self, direction: Direction) -> SideLabel:
        if direction is Direction.NEUTRAL:
            raise ValueError("a NEUTRAL direction has no side")
        return self.long if direction is Direction.UP else self.short


def _spread(bar: CandleClosed) -> Decimal:
    return MISSING_SPREAD if bar.spread is None else bar.spread


def _exits(sign: int, bar: CandleClosed) -> tuple[Decimal, Decimal, Decimal]:
    """(low, high, close) of the price this side exits at on `bar`, slippage included."""
    if sign > 0:  # a long sells at bid
        return bar.low - SLIPPAGE, bar.high - SLIPPAGE, bar.close - SLIPPAGE
    ask = _spread(bar) + SLIPPAGE  # a short buys back at ask
    return bar.low + ask, bar.high + ask, bar.close + ask


def _touch(sign: int, entry: Decimal, bar: CandleClosed, d_tp: Decimal,
           d_sl: Decimal) -> SideLabel | None:  # fmt: skip
    low, high, _ = _exits(sign, bar)
    adverse, favourable = (low, high) if sign > 0 else (high, low)
    up, down = Outcome.UP, Outcome.DOWN
    if sign * (adverse - entry) <= -d_sl:  # SL first, also when both are touched
        return SideLabel(down if sign > 0 else up, entry, entry - sign * d_sl, sign)
    if sign * (favourable - entry) >= d_tp:
        return SideLabel(up if sign > 0 else down, entry, entry + sign * d_tp, sign)
    return None


def triple_barrier(
    entry: CandleClosed,
    path: Sequence[CandleClosed],
    atr: Decimal,
    *,
    time_bars: int,
    session: SessionSchedule | None = None,
) -> TripleBarrierLabel | None:
    """Label the bet opened at `entry`'s close over the next `time_bars` expected bars
    of `path` (same symbol and timeframe); None if a bar is missing first."""
    if time_bars < 1 or atr <= 0:
        raise ValueError("need time_bars >= 1 and a positive ATR")
    d_tp, d_sl = K_TP * atr, K_SL * atr
    step = entry.close_time - entry.open_time
    by_open = {b.open_time: b for b in path}
    entries = {1: entry.close + _spread(entry) + SLIPPAGE, -1: entry.close - SLIPPAGE}
    resolved: dict[int, SideLabel] = {}
    slots = [entry.open_time]
    for _ in range(time_bars):
        slots.append(next_slot(session, slots[-1], step))
    ends_at = slots[-1] + step
    for i, slot in enumerate(slots[1:], start=1):
        bar = by_open.get(slot)
        if bar is None:
            return None  # a hole before both sides resolved: no label
        for sign, price in entries.items():
            if sign not in resolved:
                hit = _touch(sign, price, bar, d_tp, d_sl)
                if hit is None and i == time_bars:
                    hit = SideLabel(Outcome.FLAT, price, _exits(sign, bar)[2], sign)
                if hit is not None:
                    resolved[sign] = hit
        if len(resolved) == 2:  # always by the N-th bar: what is left there is FLAT
            return TripleBarrierLabel(LABEL_VERSION, d_tp, resolved[1], resolved[-1],
                                      bar.available_at, ends_at)  # fmt: skip
    raise AssertionError("unreachable: every side resolves by the N-th bar")


def time_bars(horizon: Horizon, timeframe: Timeframe) -> int:
    """The time barrier N: the horizon's span in `timeframe` bars."""
    n, rest = divmod(horizon.length, TIMEFRAME_DURATION[timeframe])
    if n < 1 or rest:
        raise ValueError(f"horizon {horizon} does not line up with {timeframe.value} bars")
    return n


def label_at(
    candles: CandleStore,
    symbol: str,
    timeframe: Timeframe,
    as_of: datetime,
    atr: Decimal | None,
    horizon: Horizon,
    *,
    now: datetime,
    session: SessionSchedule | None = None,
) -> TripleBarrierLabel | None:
    """The label of the `timeframe` bar closing at `as_of`, from bars known at `now`;
    None when the ATR, the entry bar or a bar of the window is missing (or not yet in)."""
    if atr is None or atr <= 0:
        return None
    step = TIMEFRAME_DURATION[timeframe]
    n = time_bars(horizon, timeframe)
    entry = candles.bar_at(symbol, timeframe, as_of - step, as_of=now)
    if entry is None:
        return None
    path: list[CandleClosed] = []
    slot = entry.open_time
    for _ in range(n):
        slot = next_slot(session, slot, step)
        bar = candles.bar_at(symbol, timeframe, slot, as_of=now)
        if bar is None:
            break
        path.append(bar)
    return triple_barrier(entry, path, atr, time_bars=n, session=session)


def prediction_labels(
    conn: sqlite3.Connection,
    candles: CandleStore,
    symbol: str,
    model_id: str,
    timeframe: Timeframe,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    session: SessionSchedule | None = None,
    model_version: str | None = None,
) -> list[tuple[RecordedPrediction, TripleBarrierLabel | None]]:
    """Each recorded prediction of `model_id` with `start <= as_of < end`, scored on the
    label the training export carries (LABEL_VERSION, the model's own horizon, the ATR
    of the bar's recorded M02 snapshot), from bars known at `now`; None until known."""
    log = BarLog(conn)
    atr = {r.snapshot.as_of: r.snapshot.values.get(ATR_M15)
           for r in log.snapshots(symbol, timeframe, start=start, end=end)
           if r.dataset_version == candles.dataset_version
           and r.snapshot.feature_schema == FEATURE_SCHEMA}  # fmt: skip
    out = []
    for r in log.predictions(symbol, model_id, start=start, end=end, model_version=model_version):
        h = Horizon(r.prediction.horizon, r.prediction.horizon_bars)
        lab = label_at(candles, symbol, timeframe, r.as_of, atr.get(r.as_of), h, now=now,
                       session=session)  # fmt: skip
        out.append((r, lab))
    return out
