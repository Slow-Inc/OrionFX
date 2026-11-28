"""HistData XAUUSD M1 ASCII (#128; owner decision 2026-11-28: the bulk M1 corpus). The
fetcher reads a page for its hidden `tk` token, then POSTs the form for a zip; the source
reads the one expected CSV member straight from the cached zip. Times are New York local
time WITH daylight saving (America/New_York), whatever HistData's FAQ says: a Colab
cross-check against Dukascopy (UTC) matched at +4 h in July and +5 h in January. Every
fixture is hand-made: no network, no real data."""

import io
import sqlite3
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.core.enums import Timeframe
from app.market_data.histdata import (
    GET_URL,
    MAX_ZIP,
    USER_AGENT,
    FetchFailed,
    HistDataFetcher,
    HistDataM1Source,
    Period,
    page_url,
    read_csv_member,
)
from app.market_data.history import import_history
from app.market_data.session import XAUUSD_NY_1700
from app.market_data.sources import NO_TRADE_FILL, SEALED_FROM, SOURCE_SCHEDULES, UNREAD_SLICE

D = Decimal
LATER = datetime(2030, 1, 1, tzinfo=UTC)
NY = ZoneInfo("America/New_York")
PAGE = '<form id="file_down"><input type="hidden" name="tk" id="tk" value="{tk}" /></form>'


def _csv(rows: Sequence[tuple[datetime, float]]) -> bytes:
    """HistData's format: `YYYYMMDD HHMMSS;open;high;low;close;volume`, New York wall time."""
    return "".join(
        f"{t:%Y%m%d %H%M%S};{p:.3f};{p + 0.3:.3f};{p - 0.2:.3f};{p + 0.1:.3f};0\r\n"
        for t, p in rows
    ).encode()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _year_zip(year: int, rows: Sequence[tuple[datetime, float]]) -> bytes:
    return _zip({f"DAT_ASCII_XAUUSD_M1_{year}.csv": _csv(rows),
                 f"DAT_ASCII_XAUUSD_M1_{year}.txt": b"gap report"})  # fmt: skip


class FakeSite:
    """The page answers with a token; the POST answers with a zip if the form is right."""

    def __init__(self, zips: dict[str, bytes], tk: str = "abc123") -> None:
        self.zips, self.tk = zips, tk
        self.calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str],
                 form: dict[str, str] | None) -> tuple[int, bytes]:  # fmt: skip
        self.calls.append((method, url, headers, form or {}))
        if method == "GET":
            return 200, PAGE.format(tk=self.tk).encode()
        assert form is not None and url == GET_URL
        if form.get("tk") != self.tk or headers.get("Referer") is None:
            return 200, b"<html>bad token</html>"
        body = self.zips.get(form["datemonth"])
        return (200, body) if body is not None else (404, b"")


def _fetcher(tmp_path: Path, site: FakeSite, slept: list[float]) -> HistDataFetcher:
    return HistDataFetcher(tmp_path, transport=site, sleep=slept.append)


# --- fetcher -------------------------------------------------------------------------------


def test_page_urls_for_a_year_and_a_month_of_the_current_year() -> None:
    base = ("https://www.histdata.com/download-free-forex-historical-data/"
            "?/ascii/1-minute-bar-quotes/xauusd")  # fmt: skip
    assert page_url(Period(2019)) == f"{base}/2019"
    assert page_url(Period(2026, 3)) == f"{base}/2026/3"


def test_the_fetcher_posts_the_form_with_the_token_and_caches_the_zip(tmp_path: Path) -> None:
    zipped = _year_zip(2019, [(datetime(2019, 1, 2, 9), 1280.0)])
    march = _zip({"DAT_ASCII_XAUUSD_M1_202603.csv": b""})
    site, slept = FakeSite({"2019": zipped, "202603": march}), []
    fetcher = _fetcher(tmp_path, site, slept)
    assert fetcher.fetch(Period(2019)) == zipped
    method, url, headers, form = site.calls[1]
    assert (method, url) == ("POST", GET_URL)
    assert form == {"tk": "abc123", "date": "2019", "datemonth": "2019", "platform": "ASCII",
                    "timeframe": "M1", "fxpair": "XAUUSD"}  # fmt: skip
    assert headers["Referer"] == page_url(Period(2019)) and headers["User-Agent"] == USER_AGENT
    assert "@" not in USER_AGENT
    assert fetcher.fetch(Period(2019)) == zipped and len(site.calls) == 2  # cached
    fetcher.fetch(Period(2026, 3))
    assert site.calls[3][3]["datemonth"] == "202603" and site.calls[3][3]["date"] == "2026"
    assert slept == [3.0, 3.0, 3.0]  # a pause before every request but the first
    assert (tmp_path / "XAUUSD" / "HISTDATA_XAUUSD_M1_2019.zip").read_bytes() == zipped


def test_the_polite_delay_is_at_least_three_seconds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="3"):
        HistDataFetcher(tmp_path, transport=FakeSite({}), sleep=[].append, interval=1.0)


def test_a_page_without_a_token_or_a_non_zip_answer_is_refused_and_not_cached(
    tmp_path: Path,
) -> None:
    site = FakeSite({"2019": b"<html>not a zip</html>"})
    with pytest.raises(FetchFailed, match="zip"):
        _fetcher(tmp_path, site, []).fetch(Period(2019))
    assert not list(tmp_path.rglob("*.zip"))
    site = FakeSite({}, tk="bad token!")  # outside the token's character set
    with pytest.raises(FetchFailed, match="token"):
        _fetcher(tmp_path, site, []).fetch(Period(2019))


def test_an_oversized_zip_is_refused(tmp_path: Path) -> None:
    site = FakeSite({"2019": b"PK" + b"\x00" * MAX_ZIP})
    with pytest.raises(FetchFailed, match="cap"):
        _fetcher(tmp_path, site, []).fetch(Period(2019))


# --- the zip and the CSV -----------------------------------------------------------------


def test_only_the_one_expected_csv_member_is_read() -> None:
    good = _year_zip(2019, [(datetime(2019, 1, 2, 9), 1280.0)])
    assert read_csv_member(good, Period(2019)).startswith(b"20190102 090000;")
    for members in (
        {"../DAT_ASCII_XAUUSD_M1_2019.csv": b"x"},  # path traversal
        {"DAT_ASCII_XAUUSD_M1_2018.csv": b"x"},  # the wrong period
        {"DAT_ASCII_XAUUSD_M1_2019.csv": b"x", "DAT_ASCII_XAUUSD_M1_2019 (1).csv": b"y"},
        {"readme.txt": b"x"},
    ):
        with pytest.raises(ValueError, match="member"):
            read_csv_member(_zip(members), Period(2019))


def test_a_member_larger_than_its_cap_is_refused() -> None:
    big = _zip({"DAT_ASCII_XAUUSD_M1_2019.csv": b"0" * 1000})
    with pytest.raises(ValueError, match="cap"):
        read_csv_member(big, Period(2019), max_bytes=999)


def test_new_york_local_time_converts_to_utc_with_dst_and_spread_is_none(tmp_path: Path) -> None:
    rows = [(datetime(2025, 1, 7, 15, 0), 2650.0), (datetime(2025, 7, 8, 15, 0), 3300.0)]
    path = tmp_path / "XAUUSD" / "HISTDATA_XAUUSD_M1_2025.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(_year_zip(2025, rows))
    winter, summer = HistDataM1Source(tmp_path).m1_bars(
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))  # fmt: skip
    assert winter.open_time == datetime(2025, 1, 7, 20, tzinfo=UTC)  # EST, UTC-5
    assert summer.open_time == datetime(2025, 7, 8, 19, tzinfo=UTC)  # EDT, UTC-4
    assert (winter.open, winter.high, winter.low, winter.close) == (
        D("2650.000"), D("2650.300"), D("2649.800"), D("2650.100"))  # fmt: skip
    assert winter.spread is None and winter.volume is None  # no spread; volume column is 0


def test_only_bars_in_the_requested_range_are_yielded(tmp_path: Path) -> None:
    rows = [(datetime(2025, 3, 3, 9, m), 2900.0) for m in range(5)]
    path = tmp_path / "XAUUSD" / "HISTDATA_XAUUSD_M1_2025.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(_year_zip(2025, rows))
    start = datetime(2025, 3, 3, 14, 1, tzinfo=UTC)  # 09:01 EST (before the March switch)
    bars = list(HistDataM1Source(tmp_path).m1_bars(start, start + timedelta(minutes=2)))
    assert [b.open_time for b in bars] == [start, start + timedelta(minutes=1)]


def test_the_streamed_member_is_capped_even_if_its_header_lies() -> None:
    from app.market_data.histdata import _Capped

    capped = io.BufferedReader(_Capped(io.BytesIO(b"x" * 10), 5))
    with pytest.raises(ValueError, match="cap"):
        capped.read()


def test_a_malformed_row_refuses_the_file(tmp_path: Path) -> None:
    path = tmp_path / "XAUUSD" / "HISTDATA_XAUUSD_M1_2019.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(_zip({"DAT_ASCII_XAUUSD_M1_2019.csv": b"20190102 0900;1;1;1;1;0\r\n"}))
    with pytest.raises(ValueError, match="line 1"):
        list(HistDataM1Source(tmp_path).m1_bars(datetime(2019, 1, 1, tzinfo=UTC), LATER))


# --- schedule, seal and audit -------------------------------------------------------------


def test_histdata_xauusd_is_broker_style_sealed_and_fills_no_tick_minutes() -> None:
    assert SOURCE_SCHEDULES["histdata-xauusd"] is XAUUSD_NY_1700
    assert SEALED_FROM["histdata-xauusd"] == UNREAD_SLICE[0] == datetime(2023, 11, 28, tzinfo=UTC)
    assert NO_TRADE_FILL["histdata-xauusd"] == timedelta(minutes=15)


def _day_rows(day: date, break_est: tuple[int, int]) -> list[tuple[datetime, float]]:
    """Every minute of one New York day, except the daily break in New York hours."""
    out, t = [], datetime(day.year, day.month, day.day)
    for m in range(24 * 60):
        if not break_est[0] * 60 <= m < break_est[1] * 60:
            out.append((t + timedelta(minutes=m), 2000 + m / 100))
    return out


@pytest.mark.parametrize(
    ("day", "break_est", "fits"),
    [(date(2026, 1, 7), (17, 18), True),  # winter: 17:00 EST = 22:00 UTC
     (date(2026, 7, 8), (17, 18), True),  # summer: 17:00 EDT = 21:00 UTC
     (date(2026, 7, 8), (16, 17), False)],  # a fixed-EST file would show 16-17: wrong slot
)  # fmt: skip
def test_after_conversion_the_daily_break_lands_in_the_broker_schedules_utc_slot(
    conn: sqlite3.Connection, tmp_path: Path, day: date, break_est: tuple[int, int], fits: bool
) -> None:
    rows = _day_rows(day, break_est)
    path = tmp_path / "XAUUSD" / f"HISTDATA_XAUUSD_M1_{day.year}{day.month:02d}.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(_zip({f"DAT_ASCII_XAUUSD_M1_{day.year}{day.month:02d}.csv": _csv(rows)}))
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=NY)
    start = local_midnight.astimezone(UTC)  # 00:00 New York
    end = start + timedelta(days=1)
    report = import_history(conn, HistDataM1Source(tmp_path), data_source="histdata-xauusd",
                            symbol="XAUUSD", dataset_version=f"hd-{day}-{break_est[0]}",
                            start=start, end=end, clock=lambda: LATER)  # fmt: skip
    assert report.fits is fits
    if fits:
        assert all(report.unexpected_gaps(tf) == 0 for tf in Timeframe)
    else:
        assert report.in_closed(Timeframe.H1) == 1


def test_regression_the_colab_cross_check_summer_prices_match_utc_at_plus_four(
    tmp_path: Path,
) -> None:
    """The evidence: on 2021-07-14 HistData closes matched Dukascopy's UTC closes at +4 h
    (median |diff| 0.0), not +5 h. A summer fixture whose prices are a UTC reference written
    at New York local time must read back onto that reference exactly."""
    reference = {datetime(2021, 7, 14, 14, m, tzinfo=UTC): 1800.0 + m for m in range(30)}
    rows = [((t - timedelta(hours=4)).replace(tzinfo=None), p) for t, p in reference.items()]
    path = tmp_path / "XAUUSD" / "HISTDATA_XAUUSD_M1_2021.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(_year_zip(2021, rows))
    bars = list(HistDataM1Source(tmp_path).m1_bars(datetime(2021, 1, 1, tzinfo=UTC), LATER))
    assert {b.open_time: float(b.open) for b in bars} == reference  # +4 h in summer
    winter = [(datetime(2021, 1, 13, 9, 0), 1850.0)]
    path.write_bytes(_year_zip(2021, winter))
    (bar,) = HistDataM1Source(tmp_path).m1_bars(datetime(2021, 1, 1, tzinfo=UTC), LATER)
    assert bar.open_time == datetime(2021, 1, 13, 14, 0, tzinfo=UTC)  # +5 h in winter


@pytest.mark.parametrize("stamp", ["20211107 013000", "20210314 023000"])
def test_an_ambiguous_or_missing_new_york_time_refuses_the_file(tmp_path: Path, stamp: str) -> None:
    """Both US clock changes fall on a Sunday 01:00-03:00 New York, when the market is
    closed: a row there is a data error, never guessed."""
    path = tmp_path / "XAUUSD" / "HISTDATA_XAUUSD_M1_2021.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(_zip({"DAT_ASCII_XAUUSD_M1_2021.csv": f"{stamp};1;1;1;1;0\r\n".encode()}))
    with pytest.raises(ValueError, match="line 1"):
        list(HistDataM1Source(tmp_path).m1_bars(datetime(2021, 1, 1, tzinfo=UTC), LATER))
