"""M02 slice A: time normalization, symbol mapping, candles, data quality (#29; DAT-01, DAT-04,
UST-03; M02 acceptance: DST/server time, out-of-order ticks, symbol mismatch)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.enums import DataQuality, Timeframe
from app.market_data.candles import CandleEngine, Issue
from app.market_data.port import RawTick, ReplaySource
from app.market_data.symbols import SymbolMap
from app.market_data.time import to_utc

D = Decimal
OFFSET = 120  # broker server time UTC+2 (winter)


def _tick(
    t: datetime, bid: str, ask: str | None = None, *, symbol: str = "XAUUSD.m", off: int = OFFSET
) -> RawTick:
    """`t` is UTC; the raw tick carries the broker's local wall time, as MT5 does."""
    local = (t + timedelta(minutes=off)).replace(tzinfo=None)
    return RawTick(symbol, local, off, D(bid), D(ask or bid))


def _engine() -> CandleEngine:
    return CandleEngine(SymbolMap({"XAUUSD.m": "XAUUSD"}))


T0 = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


# --- time ------------------------------------------------------------------------------------


def test_broker_time_converts_to_utc_across_dst() -> None:
    winter = to_utc(datetime(2026, 3, 27, 23, 59), 120)
    summer = to_utc(datetime(2026, 3, 30, 1, 0), 180)
    assert winter == datetime(2026, 3, 27, 21, 59, tzinfo=UTC)
    assert summer == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)


def test_aware_broker_time_is_refused() -> None:
    with pytest.raises(ValueError):
        to_utc(datetime(2026, 1, 1, tzinfo=UTC), 120)


def test_dst_switch_does_not_shift_or_duplicate_bars() -> None:
    eng = _engine()
    t = datetime(2026, 3, 29, 21, 58, tzinfo=UTC)
    eng.on_tick(_tick(t, "2000", off=120))
    # the broker clock jumps an hour forward; UTC keeps flowing
    closed = eng.on_tick(_tick(t + timedelta(minutes=3), "2001", off=180))
    (m5,) = [b for b in closed if b.timeframe is Timeframe.M5]
    assert m5.open_time == datetime(2026, 3, 29, 21, 55, tzinfo=UTC)
    assert Issue.GAP not in m5.flags


# --- symbols ---------------------------------------------------------------------------------


def test_unmapped_symbol_is_refused_and_reported() -> None:
    eng = _engine()
    assert eng.on_tick(_tick(T0, "1.08", symbol="EURUSD")) == []
    assert eng.issues[Issue.SYMBOL_MISMATCH] == 1
    assert eng.snapshot(T0).quality is DataQuality.MISSING


def test_symbol_map_is_explicit() -> None:
    m = SymbolMap({"XAUUSD.m": "XAUUSD"})
    assert m.canonical("XAUUSD.m") == "XAUUSD"
    assert m.canonical("XAUUSD") is None  # no guessing by prefix


# --- candles ---------------------------------------------------------------------------------


def test_bar_closes_only_when_the_next_interval_starts() -> None:
    eng = _engine()
    assert eng.on_tick(_tick(T0, "2000")) == []
    assert eng.on_tick(_tick(T0 + timedelta(minutes=2), "2004")) == []
    assert eng.on_tick(_tick(T0 + timedelta(minutes=4, seconds=59), "1998")) == []
    closed = eng.on_tick(_tick(T0 + timedelta(minutes=5, seconds=3), "2001"))
    (m5,) = [b for b in closed if b.timeframe is Timeframe.M5]
    assert (m5.open, m5.high, m5.low, m5.close) == (D(2000), D(2004), D(1998), D(1998))
    assert m5.close_time == T0 + timedelta(minutes=5)
    assert m5.available_at == T0 + timedelta(minutes=5, seconds=3)  # when we learned it
    assert m5.tick_count == 3
    ev = m5.as_event()
    assert ev.close_time <= ev.available_at


def test_higher_timeframes_agree_with_their_m5_bars() -> None:
    eng = _engine()
    bars = []
    price = D(2000)
    for i in range(0, 61 * 60, 30):  # one tick every 30s for just over an hour
        price += D("0.10") if (i // 300) % 2 == 0 else D("-0.07")
        bars += eng.on_tick(_tick(T0 + timedelta(seconds=i), str(price)))
    m5 = [b for b in bars if b.timeframe is Timeframe.M5]
    (h1,) = [b for b in bars if b.timeframe is Timeframe.H1]
    inside = [b for b in m5 if h1.open_time <= b.open_time < h1.close_time]
    assert len(inside) == 12
    assert h1.open == inside[0].open and h1.close == inside[-1].close
    assert h1.high == max(b.high for b in inside) and h1.low == min(b.low for b in inside)
    assert len([b for b in bars if b.timeframe is Timeframe.M15]) == 4


def test_out_of_order_and_duplicate_ticks_are_dropped_and_counted() -> None:
    eng = _engine()
    eng.on_tick(_tick(T0 + timedelta(minutes=1), "2000"))
    eng.on_tick(_tick(T0 + timedelta(minutes=1), "2000"))  # exact duplicate
    eng.on_tick(_tick(T0 + timedelta(seconds=30), "1500"))  # older than what we have
    closed = eng.on_tick(_tick(T0 + timedelta(minutes=5), "2001"))
    (m5,) = [b for b in closed if b.timeframe is Timeframe.M5]
    assert m5.low == D(2000) and m5.tick_count == 1
    assert eng.issues[Issue.OUT_OF_ORDER] == 1 and eng.issues[Issue.DUPLICATE] == 1


def test_crossed_quote_is_rejected() -> None:
    eng = _engine()
    eng.on_tick(_tick(T0, "2001", "2000"))
    assert eng.issues[Issue.INVALID_QUOTE] == 1
    assert eng.snapshot(T0).quality is DataQuality.MISSING


def test_gap_is_flagged_not_filled() -> None:
    eng = _engine()
    eng.on_tick(_tick(T0, "2000"))
    eng.on_tick(_tick(T0 + timedelta(minutes=5), "2001"))
    closed = eng.on_tick(_tick(T0 + timedelta(minutes=30), "2002"))  # 20 minutes silent
    (m5,) = [b for b in closed if b.timeframe is Timeframe.M5]
    assert m5.open_time == T0 + timedelta(minutes=5)  # no synthetic bars in between
    nxt = eng.on_tick(_tick(T0 + timedelta(minutes=35), "2003"))
    (after_gap,) = [b for b in nxt if b.timeframe is Timeframe.M5]
    assert Issue.GAP in after_gap.flags


# --- snapshot --------------------------------------------------------------------------------


def test_snapshot_reports_spread_offset_and_staleness() -> None:
    eng = _engine()
    eng.on_tick(_tick(T0, "2000.00", "2000.30"))
    snap = eng.snapshot(T0 + timedelta(seconds=1))
    assert snap.quality is DataQuality.OK and snap.spread == D("0.30")
    assert snap.broker_time_offset_minutes == OFFSET and snap.tick_time == T0
    assert eng.snapshot(T0 + timedelta(seconds=30)).quality is DataQuality.STALE


def test_replay_source_yields_ticks_after_a_cursor() -> None:
    ticks = [_tick(T0 + timedelta(seconds=i), "2000") for i in range(5)]
    src = ReplaySource(ticks)
    first = src.ticks_since(0, limit=2)
    assert [c for c, _ in first] == [1, 2]
    assert [c for c, _ in src.ticks_since(first[-1][0])] == [3, 4, 5]
