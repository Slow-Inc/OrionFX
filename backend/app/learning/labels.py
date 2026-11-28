"""Triple-barrier labels, versioned by `label_version` (#129; research backlog item 0).

`m07.labels.tb.v2`: one label per bar, the same for a long and a short, so one model
prices both sides from one (UP, DOWN, FLAT) distribution.
- The barriers sit at the entry bar's bid close +/- d, d = K x ATR_M15 with K =
  TARGET_ATR, the ATR read from the recorded snapshot at `as_of` (the value EV reads).
- The path is the bid OHLC after `as_of`. UP: a bar's high reaches the upper barrier
  first; DOWN: a bar's low reaches the lower one first; FLAT: neither within N expected
  slots of the session schedule, N being the forecast horizon's span (a scheduled
  closure is not a gap). A FLAT's `move` is its drift to the N-th close.
- No spread, slippage or commission is inside the label: it depends on bid prices only,
  so a historical source without spreads labels exactly as live bid candles do. M05's
  EV charges every cost once, outside it (`fusion.expected_value`).
- A bar touching both barriers is AMBIGUOUS (the order inside a bar is unknown): no
  class label. The export leaves such rows out; `LabelStats.of` computes the rate for a
  window (the export does not emit it yet).
- Labels are read on the finest stored timeframe that can measure them (M5 first), so
  a dual touch is rare. A missing bar before the label resolves gives no label on that
  timeframe, never a stretched one.

A PROXY, not the real trade: the bot places no per-position SL/TP (RSK-05); its exits
are the Emergency SL, the cycle Profit Target and basket trailing. Only the system
backtest that replays those exits (#131) can say whether a model is usable.

The constants are [PROPOSED]; the ones EV shares (TARGET_ATR, LABEL_VERSION) live in
`app.core.bet`. Changing any of them changes what every label means, so it needs a new
LABEL_VERSION.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.bet import LABEL_VERSION, TARGET_ATR
from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.learning.bars import BarLog, Horizon, RecordedPrediction
from app.learning.moves import FINEST_FIRST
from app.market_data.features import ATR_M15, FEATURE_SCHEMA
from app.market_data.session import SessionSchedule, next_slot
from app.market_data.store import CandleStore

__all__ = [
    "LABEL_VERSION",
    "K",
    "LabelStats",
    "Outcome",
    "TripleBarrierLabel",
    "label_at",
    "prediction_labels",
    "triple_barrier",
]

D = Decimal
K = TARGET_ATR
"""Barrier distance in ATR_M15, the same up and down: EV prices this symmetric bet."""


class Outcome(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"
    AMBIGUOUS = "AMBIGUOUS"
    """Both barriers inside one bar: no class label."""


@dataclass(frozen=True, slots=True)
class TripleBarrierLabel:
    label_version: str
    outcome: Outcome
    distance: Decimal
    """d = K x ATR_M15: each barrier's distance from the entry bid close."""
    move: Decimal | None
    """Bid move from the entry close: +d (UP), -d (DOWN), the drift to the N-th close
    (FLAT); None when AMBIGUOUS."""
    timeframe: Timeframe
    """The bars it was read on."""
    available_at: datetime
    """When it was known: the resolving (or N-th) bar's `available_at`."""
    ends_at: datetime
    """The close of the N-th expected slot: the latest price any label of this bar could
    read, whether or not it resolved earlier (a split purge bound)."""

    @property
    def labelled(self) -> bool:
        return self.outcome is not Outcome.AMBIGUOUS


def triple_barrier(
    entry: CandleClosed,
    path: Sequence[CandleClosed],
    atr: Decimal,
    *,
    time_bars: int,
    session: SessionSchedule | None = None,
) -> TripleBarrierLabel | None:
    """Label the bar `entry` over the next `time_bars` expected bars of `path` (same
    symbol and timeframe); None if a bar is missing before it resolves."""
    if time_bars < 1 or atr <= 0:
        raise ValueError("need time_bars >= 1 and a positive ATR")
    d = K * atr
    upper, lower = entry.close + d, entry.close - d
    step = entry.close_time - entry.open_time
    by_open = {b.open_time: b for b in path}
    slots = [entry.open_time]
    for _ in range(time_bars):
        slots.append(next_slot(session, slots[-1], step))
    ends_at = slots[-1] + step

    def label(outcome: Outcome, move: Decimal | None, bar: CandleClosed) -> TripleBarrierLabel:
        return TripleBarrierLabel(LABEL_VERSION, outcome, d, move, entry.timeframe,
                                  bar.available_at, ends_at)  # fmt: skip

    last = entry
    for slot in slots[1:]:
        bar = by_open.get(slot)
        if bar is None:
            return None  # a hole before it resolved: no label
        up, down = bar.high >= upper, bar.low <= lower
        if up and down:
            return label(Outcome.AMBIGUOUS, None, bar)
        if up:
            return label(Outcome.UP, d, bar)
        if down:
            return label(Outcome.DOWN, -d, bar)
        last = bar
    return label(Outcome.FLAT, last.close - entry.close, last)


def label_at(
    candles: CandleStore,
    symbol: str,
    as_of: datetime,
    atr: Decimal | None,
    horizon: Horizon,
    *,
    now: datetime,
    session: SessionSchedule | None = None,
) -> TripleBarrierLabel | None:
    """The label of the bar closing at `as_of`, from bars known at `now`, on the finest
    stored timeframe whose bars measure it (M5 first); None when the ATR is missing or
    no timeframe has the entry bar and every bar until it resolves (or not yet)."""
    if atr is None or atr <= 0:
        return None
    for tf in FINEST_FIRST:
        step = TIMEFRAME_DURATION[tf]
        n, rest = divmod(horizon.length, step)
        if n < 1 or rest:
            continue
        entry = candles.bar_at(symbol, tf, as_of - step, as_of=now)
        if entry is None:
            continue
        path: list[CandleClosed] = []
        slot = entry.open_time
        for _ in range(n):
            slot = next_slot(session, slot, step)
            bar = candles.bar_at(symbol, tf, slot, as_of=now)
            if bar is None:
                break
            path.append(bar)
        lab = triple_barrier(entry, path, atr, time_bars=n, session=session)
        if lab is not None:
            return lab
    return None


@dataclass(frozen=True, slots=True)
class LabelStats:
    """How a window's rows were labelled: the dual-touch rate and the FLAT share, to
    report per era."""

    rows: int
    missing: int
    """No label: a missing bar, a missing ATR, or not yet known."""
    ambiguous: int
    up: int
    down: int
    flat: int

    @classmethod
    def of(cls, labels: Sequence[TripleBarrierLabel | None]) -> "LabelStats":
        def count(o: Outcome) -> int:
            return sum(1 for lab in labels if lab is not None and lab.outcome is o)

        missing = sum(1 for lab in labels if lab is None)
        return cls(len(labels), missing, count(Outcome.AMBIGUOUS), count(Outcome.UP),
                   count(Outcome.DOWN), count(Outcome.FLAT))  # fmt: skip

    @property
    def dual_touch_rate(self) -> Decimal | None:
        """AMBIGUOUS over every row whose bars were known."""
        known = self.rows - self.missing
        return D(self.ambiguous) / known if known else None

    @property
    def flat_share(self) -> Decimal | None:
        """FLAT over every class-labelled row."""
        labelled = self.up + self.down + self.flat
        return D(self.flat) / labelled if labelled else None


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
    of the bar's recorded M02 snapshot on `timeframe`), from bars known at `now`."""
    log = BarLog(conn)
    atr = {r.snapshot.as_of: r.snapshot.values.get(ATR_M15)
           for r in log.snapshots(symbol, timeframe, start=start, end=end)
           if r.dataset_version == candles.dataset_version
           and r.snapshot.feature_schema == FEATURE_SCHEMA}  # fmt: skip
    out = []
    for r in log.predictions(symbol, model_id, start=start, end=end, model_version=model_version):
        h = Horizon(r.prediction.horizon, r.prediction.horizon_bars)
        lab = label_at(candles, symbol, r.as_of, atr.get(r.as_of), h, now=now, session=session)
        out.append((r, lab))
    return out
