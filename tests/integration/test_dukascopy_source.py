"""Dukascopy XAUUSD M1 candles (#128, PRD #120 slice 1; owner decision 2026-11-28). The
public datafeed serves one LZMA-compressed file of big-endian minute candles per day and
side. The decoder, the rate-limited cached fetcher and the source are tested on hand-built
bytes and a fake transport: no network, no real data."""

import lzma
import sqlite3
import struct
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import Timeframe
from app.market_data.dukascopy import (
    MAX_RAW,
    PRICE_DIVISOR,
    USER_AGENT,
    DukascopyFetcher,
    DukascopyM1Source,
    FetchFailed,
    check_day,
    day_url,
    decode_candles,
)
from app.market_data.history import import_history
from app.market_data.session import XAUUSD_NY_1700
from app.market_data.sources import NO_TRADE_FILL, SOURCE_SCHEDULES

D = Decimal
M1 = timedelta(minutes=1)
LATER = datetime(2030, 1, 1, tzinfo=UTC)


def _encode(rows: list[tuple[int, float, float, float, float, float]]) -> bytes:
    """Candles as Dukascopy stores them: (seconds into the UTC day, open, close, low,
    high, volume), prices as integers of 1/PRICE_DIVISOR, LZMA 'alone' format."""
    raw = b"".join(
        struct.pack(">5if", s, *(round(p * PRICE_DIVISOR) for p in (o, c, lo, h)), v)
        for s, o, c, lo, h, v in rows
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


class FakeTransport:
    """Serves bytes per URL; a status list per URL is consumed first (e.g. 503 then 200)."""

    def __init__(self, files: dict[str, bytes], statuses: dict[str, list[int]] | None = None):
        self.files = files
        self.statuses = statuses or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, user_agent: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, user_agent))
        queued = self.statuses.get(url)
        if queued:
            status = queued.pop(0)
            if status != 200:
                return status, b""
        if url not in self.files:
            return 404, b""
        return 200, self.files[url]


class FakeSleep:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


# --- URL and decoder -------------------------------------------------------------------


def test_the_url_uses_a_zero_based_month() -> None:
    base = "https://datafeed.dukascopy.com/datafeed/XAUUSD"
    assert day_url("XAUUSD", "BID", date(2024, 1, 2)) == f"{base}/2024/00/02/BID_candles_min_1.bi5"
    assert day_url("XAUUSD", "ASK", date(2025, 12, 31)).endswith(
        "/2025/11/31/ASK_candles_min_1.bi5"
    )


def test_the_decoder_reads_big_endian_candles_at_the_price_divisor() -> None:
    raw = _encode([(0, 2063.125, 2063.5, 2062.9, 2063.7, 1.25),
                   (60, 2063.5, 2063.4, 2063.2, 2063.6, 0.5)])  # fmt: skip
    (a, b) = decode_candles(raw, date(2024, 1, 2))
    assert a.open_time == datetime(2024, 1, 2, tzinfo=UTC)
    assert b.open_time == datetime(2024, 1, 2, 0, 1, tzinfo=UTC)
    assert (a.open, a.close, a.low, a.high) == (D("2063.125"), D("2063.5"), D("2062.9"),
                                                D("2063.7"))  # fmt: skip
    assert a.volume == D("1.25")
    assert PRICE_DIVISOR == 1000  # [PROPOSED] three decimals for XAUUSD; checked on fetch


def test_an_empty_file_is_a_day_without_data() -> None:
    assert decode_candles(b"", date(2024, 1, 6)) == []


def test_a_truncated_file_is_refused() -> None:
    raw = lzma.compress(b"\x00" * 23, format=lzma.FORMAT_ALONE)
    with pytest.raises(ValueError, match="24-byte"):
        decode_candles(raw, date(2024, 1, 2))


# --- fetcher -------------------------------------------------------------------------------


def test_the_fetcher_caches_rate_limits_and_sends_a_generic_user_agent(tmp_path: Path) -> None:
    url = day_url("XAUUSD", "BID", date(2024, 1, 2))
    transport, sleep = FakeTransport({url: b"abc"}), FakeSleep()
    fetcher = DukascopyFetcher(tmp_path, transport=transport, sleep=sleep, interval=1.5)
    assert fetcher.day_file("XAUUSD", "BID", date(2024, 1, 2)) == b"abc"
    assert fetcher.day_file("XAUUSD", "BID", date(2024, 1, 2)) == b"abc"  # from the cache
    assert len(transport.calls) == 1 and transport.calls[0][1] == USER_AGENT
    assert "@" not in USER_AGENT  # no personal data
    assert (tmp_path / "XAUUSD" / "2024" / "2024-01-02_BID.bi5").read_bytes() == b"abc"
    fetcher.day_file("XAUUSD", "ASK", date(2024, 1, 2))  # 404: cached as no data
    assert sleep.slept == [1.5]  # one pause between the two requests, none before the first
    assert fetcher.day_file("XAUUSD", "ASK", date(2024, 1, 2)) == b""
    assert len(transport.calls) == 2


def test_the_fetcher_retries_with_backoff_then_gives_up(tmp_path: Path) -> None:
    url = day_url("XAUUSD", "BID", date(2024, 1, 2))
    transport = FakeTransport({url: b"ok"}, {url: [503, 503, 200]})
    sleep = FakeSleep()
    fetcher = DukascopyFetcher(tmp_path, transport=transport, sleep=sleep, interval=1.0,
                               attempts=3, backoff=5.0)  # fmt: skip
    assert fetcher.day_file("XAUUSD", "BID", date(2024, 1, 2)) == b"ok"
    assert sleep.slept == [5.0, 10.0]  # backoff doubles; nothing is cached on a failure
    stuck = FakeTransport({}, {url: [503] * 9})
    fetcher = DukascopyFetcher(tmp_path / "x", transport=stuck, sleep=FakeSleep(), attempts=3)
    with pytest.raises(FetchFailed):
        fetcher.day_file("XAUUSD", "BID", date(2024, 1, 2))
    assert not (tmp_path / "x" / "XAUUSD" / "2024" / "2024-01-02_BID.bi5").exists()


# --- source -----------------------------------------------------------------------------


def _day(day: date, closed: tuple[int, int] | None, *, ask_over: float = 0.25,
         zero_volume: Collection[int] = frozenset()) -> dict[str, bytes]:  # fmt: skip
    """One synthetic day of bid and ask candles; `closed` is a [from, to) UTC hour range
    with no ticks (left out of the file, as on a day with a break)."""
    bid, ask = [], []
    for m in range(24 * 60):
        if closed is not None and closed[0] * 60 <= m < closed[1] * 60:
            continue
        p = 2000 + m / 100
        v = 0.0 if m in zero_volume else 1.5
        bid.append((m * 60, p, p + 0.05, p - 0.1, p + 0.1, v))
        ask.append((m * 60, p + ask_over, p + 0.05 + ask_over, p - 0.1 + ask_over,
                    p + 0.1 + ask_over, v))  # fmt: skip
    return {
        day_url("XAUUSD", "BID", day): _encode(bid),
        day_url("XAUUSD", "ASK", day): _encode(ask),
    }


def _source(tmp_path: Path, files: dict[str, bytes]) -> DukascopyM1Source:
    fetcher = DukascopyFetcher(tmp_path, transport=FakeTransport(files), sleep=FakeSleep())
    return DukascopyM1Source(fetcher)


def test_bars_are_bid_with_the_close_spread_and_zero_volume_minutes_dropped(
    tmp_path: Path,
) -> None:
    day = date(2026, 1, 7)
    source = _source(tmp_path, _day(day, None, zero_volume={1}))
    start = datetime(2026, 1, 7, tzinfo=UTC)
    bars = list(source.m1_bars(start, start + 3 * M1))
    assert [b.open_time for b in bars] == [start, start + 2 * M1]  # minute 1 had no tick
    assert bars[0].open == D("2000") and bars[0].close == D("2000.05")
    assert bars[0].spread == D("0.25")  # ask close - bid close
    assert bars[0].volume == D("1.5")


def test_saturdays_are_never_requested(tmp_path: Path) -> None:
    transport = FakeTransport({})
    source = DukascopyM1Source(DukascopyFetcher(tmp_path, transport=transport, sleep=FakeSleep()))
    list(source.m1_bars(datetime(2026, 1, 9, tzinfo=UTC), datetime(2026, 1, 12, tzinfo=UTC)))
    days = sorted({u.split("/datafeed/XAUUSD/")[1][:10] for u, _ in transport.calls})
    assert days == ["2026/00/09", "2026/00/11"]  # Friday and Sunday, not Saturday


def test_dukascopy_xauusd_uses_the_broker_style_schedule_and_fills_no_tick_minutes() -> None:
    assert SOURCE_SCHEDULES["dukascopy-xauusd"] is XAUUSD_NY_1700
    assert NO_TRADE_FILL["dukascopy-xauusd"] == timedelta(minutes=15)


@pytest.mark.parametrize(
    ("day", "break_utc", "fits"),
    [(date(2026, 1, 7), (22, 23), True),  # EST: 17:00-18:00 NY is 22:00-23:00 UTC
     (date(2026, 7, 8), (21, 22), True),  # EDT: 21:00-22:00 UTC
     (date(2026, 7, 8), (22, 23), False)],  # a fixed-UTC break does not fit in summer
)  # fmt: skip
def test_the_audit_places_the_daily_break_on_both_sides_of_us_dst(
    conn: sqlite3.Connection, tmp_path: Path, day: date, break_utc: tuple[int, int], fits: bool
) -> None:
    source = _source(tmp_path, _day(day, break_utc))
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    report = import_history(conn, source, data_source="dukascopy-xauusd", symbol="XAUUSD",
                            dataset_version=f"duka-{day}-{break_utc[0]}", start=start,
                            end=start + timedelta(days=1), clock=lambda: LATER)  # fmt: skip
    assert report.fits is fits
    if fits:
        assert all(report.unexpected_gaps(tf) == 0 for tf in Timeframe)
    else:
        assert report.in_closed(Timeframe.H1) == 1  # trading inside the NY break hour


# --- hardening before the first real fetch (remote bytes are untrusted) -----------------


def test_remote_bytes_are_capped_before_and_after_decompression() -> None:
    day = date(2024, 1, 2)
    with pytest.raises(ValueError, match="cap"):
        decode_candles(b"\x00" * (MAX_RAW + 1), day)
    bomb = lzma.compress(b"\x00" * (24 * 1441), format=lzma.FORMAT_ALONE)  # tiny when packed
    assert len(bomb) < MAX_RAW
    with pytest.raises(ValueError, match="more than 1440 records"):
        decode_candles(bomb, day)
    whole = _encode([(0, 1, 1, 1, 1, 1)])
    with pytest.raises(ValueError, match="truncated"):
        decode_candles(whole[:-6], day)


@pytest.mark.parametrize("seconds", [[30], [86400], [-60], [120, 60], [60, 60]])
def test_record_times_must_be_whole_minutes_of_the_day_in_order(seconds: list[int]) -> None:
    raw = _encode([(s, 1, 1, 1, 1, 1) for s in seconds])
    with pytest.raises(ValueError, match="minute grid or out of order"):
        decode_candles(raw, date(2024, 1, 2))


def test_symbol_and_side_are_checked_because_they_name_the_cache_path(tmp_path: Path) -> None:
    fetcher = DukascopyFetcher(tmp_path, transport=FakeTransport({}), sleep=FakeSleep())
    for symbol, side in (("../etc", "BID"), ("XAUUSD", "MID"), ("xauusd", "BID")):
        with pytest.raises(ValueError, match="bad symbol or side"):
            fetcher.day_file(symbol, side, date(2024, 1, 2))


def test_an_oversized_body_is_refused_and_not_cached(tmp_path: Path) -> None:
    url = day_url("XAUUSD", "BID", date(2024, 1, 2))
    big = FakeTransport({url: b"x" * (MAX_RAW + 1)})
    fetcher = DukascopyFetcher(tmp_path, transport=big, sleep=FakeSleep())
    with pytest.raises(FetchFailed, match="cap"):
        fetcher.day_file("XAUUSD", "BID", date(2024, 1, 2))
    assert not fetcher.path("XAUUSD", "BID", date(2024, 1, 2)).exists()


def test_the_reference_day_check_catches_a_wrong_field_order_scale_or_side() -> None:
    day = date(2020, 8, 6)
    good_bid = decode_candles(_encode([(0, 2050, 2051, 2049, 2052, 1)]), day)
    good_ask = decode_candles(_encode([(0, 2050.3, 2051.3, 2049.3, 2052.3, 1)]), day)
    lo, hi = D(1990), D(2100)
    assert check_day(good_bid, good_ask, lo, hi) == []
    swapped = decode_candles(_encode([(0, 2050, 2049, 2051, 2052, 1)]), day)  # close < low
    assert any("low <= open" in p for p in check_day(swapped, good_ask, lo, hi))
    tenfold = decode_candles(_encode([(0, 205, 205.1, 204.9, 205.2, 1)]), day)
    assert any("outside" in p for p in check_day(tenfold, good_ask, lo, hi))
    assert any("below bid" in p for p in check_day(good_ask, good_bid, lo, hi))
    assert check_day([], [], lo, hi) == ["no bid candles", "no ask candles"]
