"""Trading-session schedules (#124, PRD #120 slice 0a). The XAUUSD market closes daily
and at weekends on purpose: a technique window spanning a scheduled closure is still
contiguous, the first bar after it is not a gap, and the last bar before it stays valid
until the next expected bar closes. Holes while the market is open remain gaps."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.core.contracts import CandleClosed
from app.core.enums import DataQuality, Timeframe
from app.market_data.candles import CandleEngine
from app.market_data.features import build_feature_snapshot
from app.market_data.port import RawTick
from app.market_data.session import XAUUSD_NY_1700, SessionSchedule, audit
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


def test_a_bad_schedule_is_refused() -> None:
    with pytest.raises(ValueError):
        SessionSchedule("x", "America/New_York", daily_close=S.daily_reopen,
                        daily_reopen=S.daily_close)  # fmt: skip
    with pytest.raises(Exception):  # noqa: B017 - zoneinfo raises its own error type
        SessionSchedule("x", "Nowhere/Nothing", S.daily_close, S.daily_reopen)


def _session_bars(start: datetime, n: int, size: timedelta, *, skip: int | None = None,
                  flag_first_after_break: bool = False) -> list[CandleClosed]:  # fmt: skip
    """`n` bars on the schedule from `start`, optionally dropping the `skip`-th one."""
    out: list[CandleClosed] = []
    t, i, price = start, 0, 2000.0
    while len(out) < n:
        if i != skip:
            prev_close = out[-1].close_time if out else None
            after_break = prev_close is not None and t > prev_close
            price += 0.5 if i % 3 else -0.3
            c = D(str(round(price, 2)))
            out.append(CandleClosed(
                symbol="XAUUSD", timeframe=Timeframe.H1 if size == H1 else Timeframe.M5,
                open_time=t, close_time=t + size, open=c, high=c + 1, low=c - 1, close=c,
                volume=D(10), spread=D("0.2"), available_at=t + size + timedelta(seconds=1),
                quality_flags=["GAP"] if flag_first_after_break and after_break else [],
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


def test_the_last_bar_before_a_break_stays_valid_until_the_next_bar_closes() -> None:
    bars = _session_bars(datetime(2026, 1, 7, 0, tzinfo=UTC), 22, H1)  # last bar 21:00-22:00 UTC
    assert bars[-1].close_time == WED_JAN.replace(hour=22)
    reopen_bar_close = WED_JAN.replace(hour=23) + H1
    obs = observe(make_library(atr_n=5)["atr_5"], bars, WED_JAN.replace(hour=23, minute=30),
                  session=S)  # fmt: skip
    assert obs.valid_until == reopen_bar_close and "STALE" not in obs.quality_flags


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
    open_hole = CandleEngine("XAUUSD", {"XAUUSD": "XAUUSD"}, clock=lambda: later, session=S)
    open_hole.on_tick(_tick(WED_JAN.replace(hour=10)))
    open_hole.on_tick(_tick(WED_JAN.replace(hour=10, minute=31)))
    nxt = open_hole.on_tick(_tick(WED_JAN.replace(hour=10, minute=36)))
    assert any(c.event.quality_flags == ["GAP"] for c in nxt)


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
