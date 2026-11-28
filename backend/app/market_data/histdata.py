"""HistData.com XAUUSD M1 ASCII bars (#128, PRD #120 slice 1; DAT-01).

Owner decision 2026-11-28: free public data may be used. HistData is the **bulk M1
corpus** (Dukascopy throttles too hard for the full range and stays the measured-spread
source for the recent era). Raw zips are cached under the gitignored
`data/historical/histdata/` and never committed. Nothing here runs a download by itself:
`scripts/histdata_import.py fetch` does, on a machine where downloads are allowed.

How the site serves a file (as described by the coordinator; **[PROPOSED] until the first
real fetch confirms it**):
- The page `.../download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/xauusd/
  <YYYY>` (or `/<YYYY>/<M>` for a month of the current year) holds a form with a hidden
  `tk` token.
- The zip comes from a POST to `get.php` with `tk`, `date=<YYYY>`, `datemonth=<YYYY>` or
  `<YYYYMM>`, `platform=ASCII`, `timeframe=M1`, `fxpair=XAUUSD`, and a Referer equal to
  the page URL.
- The zip holds `DAT_ASCII_XAUUSD_M1_<period>.csv` (and a `.txt` gap report).

Format of the CSV: `YYYYMMDD HHMMSS;open;high;low;close;volume`, semicolon-separated,
**bid** prices, one row per minute with a tick. The time is **EST with no daylight
saving** (fixed UTC-5 all year), so it converts to UTC by adding five hours; in summer the
New York 17:00 break is 16:00 in the file. There is no spread column (`spread` is None, so
the backtest's stressed spread applies), and the volume column is 0 (`volume` is None
when it is 0).

Untrusted input: only the fixed https host is read (certificates verified, redirects
refused); the token must match a small character set; the response is capped at
`MAX_ZIP` and must be a zip; the zip is never extracted to disk. Only the one expected
CSV member is read, by exact name, capped at `MAX_CSV`.
"""

import csv
import io
import re
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import IO, Any

from app.market_data._http import USER_AGENT, FetchFailed, http_request, write_atomic
from app.market_data.port import M1Bar

__all__ = [
    "GET_URL",
    "MAX_ZIP",
    "USER_AGENT",
    "FetchFailed",
    "HistDataFetcher",
    "HistDataM1Source",
    "Period",
    "page_url",
    "read_csv_member",
]

SITE = "https://www.histdata.com"
PAGE_BASE = f"{SITE}/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/xauusd"
GET_URL = f"{SITE}/get.php"
MIN_INTERVAL = 3.0
"""Seconds between requests, at least: a polite client."""
MAX_ZIP = 64 * 1024 * 1024
"""A year of XAUUSD M1 is about 5 MB zipped; anything over this cap is refused."""
MAX_CSV = 256 * 1024 * 1024
"""A year of XAUUSD M1 is about 20 MB as CSV; the member is read with this cap."""
EST = timedelta(hours=5)
"""HistData's clock is UTC-5 all year: UTC = file time + 5 hours."""
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
STAMP = re.compile(r"\d{8} \d{6}")
PREFIX = "HISTDATA_XAUUSD_M1_"

Transport = Callable[[str, str, dict[str, str], dict[str, str] | None], tuple[int, bytes]]
"""(method, url, headers, form or None) -> (HTTP status, body)."""


@dataclass(frozen=True, slots=True, order=True)
class Period:
    """A year file, or a month file (`month` set) for the current year."""

    year: int
    month: int | None = None

    @property
    def tag(self) -> str:
        return f"{self.year}" if self.month is None else f"{self.year}{self.month:02d}"

    def utc_span(self) -> tuple[datetime, datetime]:
        """[start, end) of the period's bars in UTC (file time is EST, UTC-5)."""
        if self.month is None:
            a, b = datetime(self.year, 1, 1), datetime(self.year + 1, 1, 1)
        else:
            a = datetime(self.year, self.month, 1)
            b = datetime(self.year + self.month // 12, self.month % 12 + 1, 1)
        return a.replace(tzinfo=UTC) + EST, b.replace(tzinfo=UTC) + EST

    @classmethod
    def from_tag(cls, tag: str) -> "Period":
        if re.fullmatch(r"\d{4}", tag):
            return cls(int(tag))
        if re.fullmatch(r"\d{6}", tag) and 1 <= int(tag[4:]) <= 12:
            return cls(int(tag[:4]), int(tag[4:]))
        raise ValueError(f"not a HistData period: {tag!r}")


def page_url(period: Period) -> str:
    return f"{PAGE_BASE}/{period.year}" + ("" if period.month is None else f"/{period.month}")


class _TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "input" and "tk" in (a.get("name"), a.get("id")) and self.token is None:
            self.token = a.get("value")


def _token(page: bytes) -> str:
    parser = _TokenParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    if parser.token is None or not TOKEN.fullmatch(parser.token):
        raise FetchFailed("the page has no usable tk token")
    return parser.token


def _zip_path(cache_dir: Path, period: Period) -> Path:
    return cache_dir / "XAUUSD" / f"{PREFIX}{period.tag}.zip"


def _member(z: zipfile.ZipFile, period: Period, max_bytes: int) -> zipfile.ZipInfo:
    """The one expected CSV member, by exact name (no other CSV, no path in the name)."""
    name = f"DAT_ASCII_XAUUSD_M1_{period.tag}.csv"
    csvs = [i for i in z.infolist() if i.filename.lower().endswith(".csv")]
    if len(csvs) != 1 or csvs[0].filename != name:
        raise ValueError(f"expected exactly one member {name}, found "
                         f"{[i.filename for i in csvs]}")  # fmt: skip
    if csvs[0].file_size > max_bytes:
        raise ValueError(f"{name}: {csvs[0].file_size} bytes is over the {max_bytes} cap")
    return csvs[0]


class _Capped(io.RawIOBase):
    """A read-only stream that refuses to yield more than `cap` bytes (the zip header's
    size can lie)."""

    def __init__(self, raw: IO[bytes], cap: int) -> None:  # a zipfile member stream
        self._raw, self._left = raw, cap

    def readable(self) -> bool:
        return True

    def readinto(self, buf: Any) -> int:
        data = self._raw.read(len(buf))
        n = len(data)
        buf[:n] = data
        self._left -= n
        if self._left < 0:
            raise ValueError(f"the CSV member is over the {MAX_CSV}-byte cap")
        return n


def read_csv_member(zipped: bytes, period: Period, *, max_bytes: int = MAX_CSV) -> bytes:
    """The bytes of the one expected CSV member, read in memory (never extracted)."""
    with zipfile.ZipFile(io.BytesIO(zipped)) as z, z.open(_member(z, period, max_bytes)) as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{period.tag}: the CSV member is over the {max_bytes}-byte cap")
    return data


def _urllib(
    method: str, url: str, headers: dict[str, str], form: dict[str, str] | None
) -> tuple[int, bytes]:
    return http_request(method, url, headers, form, cap=MAX_ZIP, timeout=120)


class HistDataFetcher:
    """Period zips, from the cache or (politely, one request at a time) from the site."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        transport: Transport = _urllib,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = MIN_INTERVAL,
        attempts: int = 3,
        backoff: float = 30.0,
    ) -> None:
        if interval < MIN_INTERVAL:
            raise ValueError(f"the delay between requests must be at least {MIN_INTERVAL} s")
        self.cache_dir = cache_dir
        self._transport, self._sleep = transport, sleep
        self._interval, self._attempts, self._backoff = interval, attempts, backoff
        self.requests = 0

    def path(self, period: Period) -> Path:
        return _zip_path(self.cache_dir, period)

    def fetch(self, period: Period) -> bytes:
        path = self.path(period)
        if path.exists():
            return path.read_bytes()
        referer = page_url(period)
        headers = {"User-Agent": USER_AGENT}
        tk = _token(self._request("GET", referer, headers, None))
        form = {"tk": tk, "date": str(period.year), "datemonth": period.tag,
                "platform": "ASCII", "timeframe": "M1", "fxpair": "XAUUSD"}  # fmt: skip
        body = self._request("POST", GET_URL, {**headers, "Referer": referer}, form)
        if len(body) > MAX_ZIP:
            raise FetchFailed(f"{period.tag}: the zip is over the {MAX_ZIP}-byte cap")
        try:
            read_csv_member(body, period)  # a zip holding the expected member, or refused
        except (zipfile.BadZipFile, ValueError) as exc:
            raise FetchFailed(f"{period.tag}: the answer is not the expected zip ({exc})") from exc
        write_atomic(path, body)
        return body

    def _request(
        self, method: str, url: str, headers: dict[str, str], form: dict[str, str] | None
    ) -> bytes:
        status = 0
        for attempt in range(self._attempts):
            if self.requests:
                self._sleep(self._interval if not attempt else self._backoff * 2 ** (attempt - 1))
            self.requests += 1
            status, body = self._transport(method, url, headers, form)
            if status == 200:
                return body
            if status == 404:
                break
        raise FetchFailed(f"{method} {url}: HTTP {status}")


class HistDataM1Source:
    """M1 bars from the cached zips, in time order; reads no network."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache = cache_dir

    def periods(self) -> list[Period]:
        return sorted(Period.from_tag(p.stem.removeprefix(PREFIX))
                      for p in (self._cache / "XAUUSD").glob(f"{PREFIX}*.zip"))  # fmt: skip

    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        for period in self.periods():
            a, b = period.utc_span()
            if b <= start or end <= a:
                continue
            # Streamed from the zip, never extracted and never held whole in memory.
            with (
                zipfile.ZipFile(_zip_path(self._cache, period)) as z,
                z.open(_member(z, period, MAX_CSV)) as raw,
            ):
                text = io.TextIOWrapper(io.BufferedReader(_Capped(raw, MAX_CSV)),
                                        encoding="ascii", newline="")  # fmt: skip
                for line, row in enumerate(csv.reader(text, delimiter=";"), 1):
                    bar = _parse(row, line, period, start, end)
                    if bar is not None:
                        yield bar


def _parse(row: list[str], line: int, period: Period, start: datetime, end: datetime
           ) -> M1Bar | None:  # fmt: skip
    """The row's bar, or None outside [start, end) (checked before any price is parsed)."""
    try:
        t, o, h, lo, c, v = row
        if not STAMP.fullmatch(t):
            raise ValueError(f"time {t!r} is not YYYYMMDD HHMMSS")
        at = datetime.fromisoformat(t).replace(tzinfo=UTC) + EST
        if not start <= at < end:
            return None
        volume = Decimal(v)
        return M1Bar(at, Decimal(o), Decimal(h), Decimal(lo), Decimal(c),
                     None if volume == 0 else volume, spread=None)  # fmt: skip
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"HistData {period.tag} line {line}: {exc}") from exc
