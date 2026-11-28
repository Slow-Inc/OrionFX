"""Trading-session schedules (#124, PRD #120 slice 0a). The XAUUSD market closes daily
and at weekends on purpose: a technique window spanning a scheduled closure is still
contiguous, the first bar after it is not a gap, and the last bar before it stays valid
only until the market reopens. Holes while the market is open remain gaps."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.core.contracts import CandleClosed
from app.core.enums import DataQuality, Timeframe
from app.market_data.candles import CandleEngine
from app.market_data.features import build_feature_snapshot
from app.market_data.port import RawTick
from app.market_data.session import CME_GLOBEX_GC, XAUUSD_NY_1700, SessionSchedule, audit
from app.market_data.techniques.indicators import make_library, observe

D = Decimal
S = XAUUSD_NY_1700
H1 = timedelta(hours=1)
M5 = timedelta(minutes=5)
# New York is UTC-5 in January (EST) and UTC-4 in July (EDT).
WED_JAN = datetime(2026, 1, 7, tzinfo=UTC)
WED_JUL = datetime(2026, 7, 8, tzinfo=UTC)


def test_the_daily_break_follows_new_york_time_across_dst() -> None:
    assert S.is_open(WED_JAN.replace(hour=21, minute=55))
    assert not S.is_open(WED_JAN.replace(hour=22))  # 17:00 EST
    assert S.is_open(WED_JAN.replace(hour=23))  # 18:00 EST
    assert not S.is_open(WED_JUL.replace(hour=21))  # 17:00 EDT
    assert S.is_open(WED_JUL.replace(hour=22))


def test_the_weekend_is_closed_from_friday_close_to_sunday_reopen() -> None:
    fri = datetime(2026, 1, 9, 21, 55, tzinfo=UTC)
    assert S.is_open(fri)
    assert not S.is_open(fri + M5)  # Fri 17:00 EST
    assert not S.is_open(datetime(2026, 1, 10, 12, tzinfo=UTC))  # Saturday
    assert not S.is_open(datetime(2026, 1, 11, 22, 55, tzinfo=UTC))  # Sun 17:55 EST
    assert S.is_open(datetime(2026, 1, 11, 23, tzinfo=UTC))  # Sun 18:00 EST


def test_the_next_expected_bar_skips_scheduled_closures_only() -> None:
    assert S.next_open_slot(WED_JAN.replace(hour=20), H1) == WED_JAN.replace(hour=21)
    assert S.next_open_slot(WED_JAN.replace(hour=21), H1) == WED_JAN.replace(hour=23)
    fri_last = datetime(2026, 1, 9, 21, tzinfo=UTC)
    assert S.next_open_slot(fri_last, H1) == datetime(2026, 1, 11, 23, tzinfo=UTC)
    assert S.next_open_slot(WED_JAN.replace(hour=21, minute=55), M5) == WED_JAN.replace(hour=23)


def test_a_schedule_off_the_5_minute_grid_is_refused() -> None:
    with pytest.raises(ValueError, match="5-minute"):
        SessionSchedule("x", "America/New_York", time(17, 3), time(18, 0))


def test_m15_windows_span_the_break_and_dst_weekends() -> None:
    m15 = timedelta(minutes=15)
    # the US spring-forward weekend: Sunday 2026-03-08 reopens at 22:00 UTC (18:00 EDT)
    assert S.next_open_slot(datetime(2026, 3, 6, 21, 45, tzinfo=UTC), m15) == datetime(
        2026, 3, 8, 22, 0, tzinfo=UTC)  # fmt: skip
    assert S.next_open_slot(datetime(2026, 10, 30, 20, 45, tzinfo=UTC), m15) == datetime(
        2026, 11, 1, 23, 0, tzinfo=UTC)  # fall back: 18:00 EST  # fmt: skip
    t, bars = datetime(2026, 3, 5, 12, tzinfo=UTC), []
    while len(bars) < 120:
        c = D(2000) + D(len(bars) % 7)
        bars.append(CandleClosed(symbol="XAUUSD", timeframe=Timeframe.M15, open_time=t,
                                 close_time=t + m15, open=c, high=c + 1, low=c - 1, close=c,
                                 volume=D(5), available_at=t + m15))  # fmt: skip
        t = S.next_open_slot(t, m15)
    obs = observe(make_library(atr_n=10)["atr_10"], bars, bars[-1].available_at, session=S)
    assert "atr" in obs.values


def test_a_bad_schedule_is_refused() -> None:
    with pytest.raises(ValueError):
        SessionSchedule("x", "America/New_York", daily_close=S.daily_reopen,
                        daily_reopen=S.daily_close)  # fmt: skip
    with pytest.raises(Exception):  # noqa: B017 - zoneinfo raises its own error type
        SessionSchedule("x", "Nowhere/Nothing", S.daily_close, S.daily_reopen)


def _session_bars(
    start: datetime, n: int, size: timedelta, *, skip: int | None = None
) -> list[CandleClosed]:
    """`n` bars on the schedule from `start`, optionally dropping the `skip`-th one."""
    out: list[CandleClosed] = []
    t, i, price = start, 0, 2000.0
    while len(out) < n:
        if i != skip:
            price += 0.5 if i % 3 else -0.3
            c = D(str(round(price, 2)))
            out.append(CandleClosed(
                symbol="XAUUSD", timeframe=Timeframe.H1 if size == H1 else Timeframe.M5,
                open_time=t, close_time=t + size, open=c, high=c + 1, low=c - 1, close=c,
                volume=D(10), spread=D("0.2"), available_at=t + size + timedelta(seconds=1),
                quality_flags=[],
            ))  # fmt: skip
        i += 1
        t = S.next_open_slot(t, size)
    return out


def test_a_window_spanning_the_daily_break_and_a_weekend_is_usable() -> None:
    bars = _session_bars(datetime(2026, 1, 8, 0, tzinfo=UTC), 60, H1)  # spans Fri→Sun
    atr = make_library(atr_n=10)["atr_10"]  # lookback 51 of 60 bars
    assert any(b.open_time - a.open_time > H1 for a, b in pairwise(bars))
    now = bars[-1].available_at
    assert observe(atr, bars, now).values == {}  # strict 24/7 reading: a gap
    obs = observe(atr, bars, now, session=S)
    assert "atr" in obs.values and "GAP_IN_WINDOW" not in obs.quality_flags


def test_a_hole_while_the_market_is_open_is_still_a_gap() -> None:
    bars = _session_bars(datetime(2026, 1, 6, 0, tzinfo=UTC), 30, H1, skip=10)
    obs = observe(make_library(atr_n=5)["atr_5"], bars, bars[-1].available_at, session=S)
    assert "WARMUP" not in obs.quality_flags
    assert obs.values == {} and "GAP_IN_WINDOW" in obs.quality_flags


def test_the_last_bar_before_a_break_is_valid_only_until_the_market_reopens() -> None:
    bars = _session_bars(datetime(2026, 1, 7, 0, tzinfo=UTC), 22, H1)  # last bar 21:00-22:00 UTC
    assert bars[-1].close_time == WED_JAN.replace(hour=22)
    atr = make_library(atr_n=5)["atr_5"]
    during = observe(atr, bars, WED_JAN.replace(hour=22, minute=30), session=S)
    assert during.valid_until == WED_JAN.replace(hour=23) and "STALE" not in during.quality_flags
    after = observe(atr, bars, WED_JAN.replace(hour=23, minute=30), session=S)
    assert "STALE" in after.quality_flags  # Wednesday's bar is not fresh after the reopen


def test_a_snapshot_across_a_weekend_is_ok_with_the_schedule() -> None:
    h1 = _session_bars(datetime(2026, 1, 1, 0, tzinfo=UTC), 160, H1)
    now = h1[-1].available_at
    snap = build_feature_snapshot("XAUUSD", {Timeframe.H1: h1}, now, session=S)
    assert snap.quality is DataQuality.OK
    assert build_feature_snapshot("XAUUSD", {Timeframe.H1: h1}, now).quality is not DataQuality.OK


def _tick(t: datetime, bid: str = "2000") -> RawTick:
    return RawTick("XAUUSD", t.replace(tzinfo=None), 0, D(bid), D(bid) + D("0.2"))


def test_the_candle_engine_flags_open_market_holes_but_not_scheduled_breaks() -> None:
    later = datetime(2027, 1, 1, tzinfo=UTC)
    before_break = WED_JAN.replace(hour=21, minute=58)
    after_break = WED_JAN.replace(hour=23, minute=1)
    eng = CandleEngine("XAUUSD", {"XAUUSD": "XAUUSD"}, clock=lambda: later, session=S)
    eng.on_tick(_tick(before_break))
    closed = eng.on_tick(_tick(after_break))
    assert closed and all(c.event.quality_flags == [] for c in closed)
    strict = CandleEngine("XAUUSD", {"XAUUSD": "XAUUSD"}, clock=lambda: later)
    strict.on_tick(_tick(before_break))
    strict.on_tick(_tick(after_break))
    nxt = strict.on_tick(_tick(after_break + M5))
    assert any(c.event.quality_flags == ["GAP"] for c in nxt)
    first_after = eng.on_tick(_tick(after_break + M5))  # closes the first bar after the break
    assert first_after and all(c.event.quality_flags == [] for c in first_after)
    open_hole = CandleEngine("XAUUSD", {"XAUUSD": "XAUUSD"}, clock=lambda: later, session=S)
    open_hole.on_tick(_tick(WED_JAN.replace(hour=10)))
    open_hole.on_tick(_tick(WED_JAN.replace(hour=10, minute=31)))
    nxt = open_hole.on_tick(_tick(WED_JAN.replace(hour=10, minute=36)))
    assert any(c.event.quality_flags == ["GAP"] for c in nxt)


def test_a_higher_bar_cut_by_a_scheduled_close_is_not_a_gap() -> None:
    # Closes 17:30 New York (22:30 UTC in January): H1 22:00 is half open, half closed.
    half = SessionSchedule("x", "America/New_York", time(17, 30), time(18, 0))
    later = datetime(2027, 1, 1, tzinfo=UTC)
    eng = CandleEngine("XAUUSD", {"XAUUSD": "XAUUSD"}, clock=lambda: later, session=half)
    for minute in range(30):
        eng.on_tick(_tick(WED_JAN.replace(hour=22, minute=minute)))
    closed = eng.on_tick(_tick(WED_JAN.replace(hour=23, minute=1)))
    (h1,) = [c.event for c in closed if c.event.timeframe is Timeframe.H1]
    assert h1.quality_flags == []


def test_audit_finds_bars_in_closed_time_and_real_gaps() -> None:
    bars = _session_bars(datetime(2026, 1, 6, 0, tzinfo=UTC), 40, H1, skip=5)
    good = audit(S, bars)
    assert good.fits and good.unexpected_gaps == 1 and good.bars == 40
    t = WED_JAN.replace(hour=22)  # inside the daily break
    c = D(2000)
    stray = CandleClosed(symbol="XAUUSD", timeframe=Timeframe.H1, open_time=t,
                         close_time=t + H1, open=c, high=c, low=c, close=c, volume=None,
                         available_at=t + H1)  # fmt: skip
    assert not audit(S, [*bars, stray]).fits


def test_the_cme_globex_gold_schedule_closes_17_to_18_new_york_time_across_dst() -> None:
    """CME Globex GC: Sunday-Friday 18:00-17:00 ET with a 60-minute break from 17:00 ET
    (FirstRate's FAQ: "6pm US Eastern on the previous calendar day to 5pm")."""
    g = CME_GLOBEX_GC
    assert g.schedule_id == "cme-globex-gc-et" and g.tz == "America/New_York"
    assert g.is_open(WED_JAN.replace(hour=21, minute=59))  # 16:59 EST
    assert not g.is_open(WED_JAN.replace(hour=22))  # 17:00 EST
    assert g.is_open(WED_JAN.replace(hour=23))  # 18:00 EST
    assert not g.is_open(WED_JUL.replace(hour=21, minute=30))  # 17:30 EDT
    assert g.is_open(WED_JUL.replace(hour=22))  # 18:00 EDT
    assert not g.is_open(datetime(2026, 1, 9, 22, tzinfo=UTC))  # Friday 17:00 EST
    assert not g.is_open(datetime(2026, 1, 11, 22, 55, tzinfo=UTC))  # Sunday 17:55 EST
    assert g.is_open(datetime(2026, 1, 11, 23, tzinfo=UTC))  # Sunday 18:00 EST
