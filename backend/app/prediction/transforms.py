"""The M04 stationary transform path, schema `m04.transforms.v1` (#129; research backlog
item 1).

`transform` is the one function that turns an M02 feature snapshot, and the bars it was
built from, into model inputs that do not depend on the gold price level. Training
(`export_dataset(..., transforms=True)`) and live prediction (`MarketRecorder` for a
model whose card reads TRANSFORM_SCHEMA) both call it, so offline and online features
cannot drift. Its output is a `FeatureSnapshot` of TRANSFORM_SCHEMA with the M02
snapshot's times and quality, so `predict` gates it like any snapshot.

Point in time: only bars available at the snapshot's `available_at` are read. Every
value is a ratio of price differences, a bounded index or a time encoding, and is None
when an input is missing (never 0):
- per timeframe: (close - SMA)/ATR, Bollinger position, MACD/ATR, MACD histogram/ATR,
  ATR/close, the log return over the last contiguous bar, spread/ATR, and RSI, ADX, +DI
  and -DI scaled to 0..1. ATR/close and the log return are relative to the price level:
  invariant to a rescaled price, not to a shifted one (LEVEL_RELATIVE);
- the UTC hour of the snapshot as sin/cos;
- session and liquidity features from the finest timeframe's gold bars alone: the
  distance from the close to the previous session's high and low in ATR_M15, the
  previous session's London/New York volatility ratio (mean bar range in each city's
  08:00-17:00 local time, so DST is handled), and the spread over its rolling median.
  A session is the trading day between scheduled reopens (the `SessionSchedule`), or
  the UTC day without one; the previous session is the last completed one, and its
  features are None unless the bars cover it from its start.
Tick volume is left out: it is a broker tick count, not comparable across sources.
Windows and session hours are [PROPOSED].
"""

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.contracts import CandleClosed, FeatureSnapshot
from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.market_data.features import ATR_M15, FEATURE_SCHEMA, feature_key
from app.market_data.session import SessionSchedule, next_slot
from app.market_data.store import CandleStore

__all__ = [
    "LEVEL_RELATIVE",
    "SESSION_LOOKBACK",
    "SPREAD_WINDOW",
    "TRANSFORM_SCHEMA",
    "snapshot_timeframes",
    "transform",
    "transform_from_store",
    "transform_keys",
]

D = Decimal
TRANSFORM_SCHEMA = "m04.transforms.v1"
SESSION_LOOKBACK = 600
"""Finest-timeframe M5 bars a caller passes so the previous session is covered: two
23-hour sessions are 552 M5 bars [PROPOSED]."""
SPREAD_WINDOW = 48
"""Bars in the rolling spread median [PROPOSED]."""
LONDON = ("Europe/London", time(8), time(17))
NEW_YORK = ("America/New_York", time(8), time(17))
Q = D("1e-12")
"""Every value is quantised, so exported bytes are deterministic."""

_PER_TF = ("close_sma_atr", "bb_position", "macd_atr", "macd_hist_atr", "atr_close",
           "log_return", "spread_atr", "rsi", "adx", "plus_di", "minus_di")  # fmt: skip
_SHARED = ("time.hour_sin", "time.hour_cos", "session.prev_high_atr", "session.prev_low_atr",
           "session.london_ny_vol", "session.spread_anomaly")  # fmt: skip
LEVEL_RELATIVE = frozenset(f"{tf}.{k}" for tf in Timeframe for k in ("atr_close", "log_return"))

Values = dict[str, Decimal | None]


def _finest_first(timeframes: Iterable[Timeframe]) -> list[Timeframe]:
    return sorted(timeframes, key=lambda tf: TIMEFRAME_DURATION[tf])


def transform_keys(timeframes: Iterable[Timeframe]) -> list[str]:
    """The output keys, in order, for snapshots of these timeframes."""
    return [f"{tf}.{k}" for tf in _finest_first(timeframes) for k in _PER_TF] + list(_SHARED)


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None or b == 0 else a / b


def _sub(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a - b


def _per_tf(tf: Timeframe, bars: Sequence[CandleClosed], snap: Mapping[str, Decimal | None],
            session: SessionSchedule | None) -> Values:  # fmt: skip
    def v(catalog_id: str, output: str) -> Decimal | None:
        return snap.get(feature_key(tf, catalog_id, output))

    if not bars:
        return dict.fromkeys(_PER_TF)
    last, close = bars[-1], bars[-1].close
    atr = v("CAT-T01-06", "atr")
    upper, lower = v("CAT-T01-09", "upper"), v("CAT-T01-09", "lower")
    contiguous = len(bars) > 1 and last.open_time == next_slot(
        session, bars[-2].open_time, TIMEFRAME_DURATION[tf]
    )
    return {
        "close_sma_atr": _div(_sub(close, v("CAT-T01-01", "sma")), atr),
        "bb_position": _div(_sub(close, lower), _sub(upper, lower)),
        "macd_atr": _div(v("CAT-T01-05", "macd"), atr),
        "macd_hist_atr": _div(v("CAT-T01-05", "histogram"), atr),
        "atr_close": _div(atr, close),
        "log_return": (close / bars[-2].close).ln() if contiguous else None,
        "spread_atr": _div(last.spread, atr),
        "rsi": _div(v("CAT-T01-04", "rsi"), D(100)),
        "adx": _div(v("CAT-T01-07", "adx"), D(100)),
        "plus_di": _div(v("CAT-T01-08", "plus_di"), D(100)),
        "minus_di": _div(v("CAT-T01-08", "minus_di"), D(100)),
    }


def _session_start(t: datetime, session: SessionSchedule | None) -> datetime:
    """The start of the session holding `t`: the latest scheduled reopen at or before it
    on which the market opened (the UTC midnight without a schedule)."""
    if session is None:
        return datetime.combine(t.astimezone(UTC).date(), time(0), tzinfo=UTC)
    tz = ZoneInfo(session.tz)
    local = t.astimezone(tz)
    start = datetime.combine(local.date(), session.daily_reopen, tzinfo=tz)
    if start > local:
        start -= timedelta(days=1)
    for _ in range(7):
        if session.is_open(start):
            return start.astimezone(UTC)
        start -= timedelta(days=1)  # wall-clock arithmetic: stays on the local reopen
    raise ValueError(f"{session.schedule_id}: no reopen within a week of {t.isoformat()}")


def _mean_range(bars: Sequence[CandleClosed], hours: tuple[str, time, time]) -> Decimal | None:
    tz, start, end = ZoneInfo(hours[0]), hours[1], hours[2]
    ranges = [b.high - b.low for b in bars if start <= b.open_time.astimezone(tz).time() < end]
    return sum(ranges, D(0)) / len(ranges) if ranges else None


def _session(bars: Sequence[CandleClosed], atr_m15: Decimal | None,
             session: SessionSchedule | None) -> Values:  # fmt: skip
    out: Values = dict.fromkeys(_SHARED[2:])
    if not bars:
        return out
    last = bars[-1]
    window = bars[-SPREAD_WINDOW:]
    spreads = [b.spread for b in window if b.spread is not None]
    if len(window) == SPREAD_WINDOW and last.spread is not None:
        out["session.spread_anomaly"] = _div(last.spread, statistics.median(spreads))
    current = _session_start(last.open_time, session)
    previous = _session_start(current - timedelta(minutes=1), session)
    day = [b for b in bars if previous <= b.open_time < current]
    if bars[0].open_time > previous or not day:
        return out  # the window does not reach back over the whole previous session
    out["session.prev_high_atr"] = _div(last.close - max(b.high for b in day), atr_m15)
    out["session.prev_low_atr"] = _div(last.close - min(b.low for b in day), atr_m15)
    out["session.london_ny_vol"] = _div(_mean_range(day, LONDON), _mean_range(day, NEW_YORK))
    return out


def transform(
    snapshot: FeatureSnapshot,
    bars_by_tf: Mapping[Timeframe, Sequence[CandleClosed]],
    *,
    session: SessionSchedule | None = None,
) -> FeatureSnapshot:
    """The TRANSFORM_SCHEMA snapshot of an M02 `snapshot` and the bars it was built from
    (per timeframe; the finest gets SESSION_LOOKBACK bars for the session features)."""
    if snapshot.feature_schema != FEATURE_SCHEMA:
        raise ValueError(f"transforms read an m02 snapshot ({FEATURE_SCHEMA}), "
                         f"not {snapshot.feature_schema}")  # fmt: skip
    tfs = _finest_first(bars_by_tf)
    known = {tf: sorted((b for b in bars_by_tf[tf] if b.available_at <= snapshot.available_at),
                        key=lambda b: b.open_time) for tf in tfs}  # fmt: skip
    values: Values = {}
    for tf in tfs:
        values.update({f"{tf}.{k}": x for k, x in _per_tf(tf, known[tf], snapshot.values,
                                                          session).items()})  # fmt: skip
    t = snapshot.as_of.astimezone(UTC)
    angle = 2 * math.pi * (t.hour + t.minute / 60) / 24
    values["time.hour_sin"] = D(math.sin(angle))
    values["time.hour_cos"] = D(math.cos(angle))
    values.update(_session(known[tfs[0]] if tfs else [], snapshot.values.get(ATR_M15), session))
    return snapshot.model_copy(update={
        "feature_schema": TRANSFORM_SCHEMA,
        "values": {k: None if values[k] is None else values[k].quantize(Q)  # type: ignore[union-attr]
                   for k in transform_keys(tfs)},
    })  # fmt: skip


def snapshot_timeframes(snapshot: FeatureSnapshot) -> list[Timeframe]:
    """The timeframes an M02 snapshot holds features of, finest first."""
    return _finest_first(
        tf for tf in Timeframe if any(k.startswith(f"{tf}.") for k in snapshot.values)
    )


def transform_from_store(
    candles: CandleStore, snapshot: FeatureSnapshot, *, session: SessionSchedule | None = None
) -> FeatureSnapshot:
    """`transform` over the bars `candles` held when `snapshot` was built: the one way
    both the recorder (live) and the dataset export (training) read them."""
    tfs = snapshot_timeframes(snapshot)  # only the finest feeds the session features
    bars = {tf: candles.bars(snapshot.symbol, tf, as_of=snapshot.available_at,
                             limit=SESSION_LOOKBACK if tf == tfs[0] else 2)
            for tf in tfs}  # fmt: skip
    return transform(snapshot, bars, session=session)
