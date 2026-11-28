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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

from app.market_data.port import M1Bar

__all__ = [
    "GET_URL",
    "MAX_CSV",
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
USER_AGENT = "Mozilla/5.0 (compatible; historical-data-client/1.0)"
MIN_INTERVAL = 3.0
"""Seconds between requests, at least: a polite client."""
MAX_ZIP = 64 * 1024 * 1024
"""A year of XAUUSD M1 is about 5 MB zipped; anything over this cap is refused."""
MAX_CSV = 256 * 1024 * 1024
"""A year of XAUUSD M1 is about 20 MB as CSV; the member is read with this cap."""
EST = timedelta(hours=5)
"""HistData's clock is UTC-5 all year: UTC = file time + 5 hours."""
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
TIME_FORMAT = "%Y%m%d %H%M%S"
STAMP = re.compile(r"\d{8} \d{6}")

Transport = Callable[[str, str, dict[str, str], dict[str, str] | None], tuple[int, bytes]]
"""(method, url, headers, form or None) -> (HTTP status, body)."""


class FetchFailed(RuntimeError):
    pass


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


def read_csv_member(zipped: bytes, period: Period, *, max_bytes: int = MAX_CSV) -> bytes:
    """The bytes of the one expected CSV member, read in memory (never extracted)."""
    name = f"DAT_ASCII_XAUUSD_M1_{period.tag}.csv"
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        csvs = [i for i in z.infolist() if i.filename.lower().endswith(".csv")]
        if len(csvs) != 1 or csvs[0].filename != name:
            raise ValueError(f"expected exactly one member {name}, found "
                             f"{[i.filename for i in csvs]}")  # fmt: skip
        if csvs[0].file_size > max_bytes:
            raise ValueError(f"{name}: {csvs[0].file_size} bytes is over the {max_bytes} cap")
        with z.open(csvs[0]) as f:
            data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{name}: over the {max_bytes}-byte cap")
    return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # a 3xx comes back as an HTTP error: only the fixed host is read


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib(
    method: str, url: str, headers: dict[str, str], form: dict[str, str] | None
) -> tuple[int, bytes]:
    data = None if form is None else urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=120) as r:
            return r.status, r.read(MAX_ZIP + 1)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


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
        return self.cache_dir / "XAUUSD" / f"HISTDATA_XAUUSD_M1_{period.tag}.zip"

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
        if not body.startswith(b"PK") or not zipfile.is_zipfile(io.BytesIO(body)):
            raise FetchFailed(f"{period.tag}: the answer is not a zip")
        read_csv_member(body, period)  # refuse a zip without the expected member
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
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
        self._dir = cache_dir / "XAUUSD"

    def periods(self) -> list[Period]:
        found = []
        for p in sorted(self._dir.glob("HISTDATA_XAUUSD_M1_*.zip")):
            found.append(Period.from_tag(p.stem.removeprefix("HISTDATA_XAUUSD_M1_")))
        return sorted(found)

    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        for period in self.periods():
            a, b = period.utc_span()
            if b <= start or end <= a:
                continue
            path = self._dir / f"HISTDATA_XAUUSD_M1_{period.tag}.zip"
            data = read_csv_member(path.read_bytes(), period)
            for line, row in enumerate(csv.reader(io.StringIO(data.decode()), delimiter=";"), 1):
                bar = _parse(row, line, period)
                if start <= bar.open_time < end:
                    yield bar


def _parse(row: list[str], line: int, period: Period) -> M1Bar:
    try:
        t, o, h, lo, c, v = row
        if not STAMP.fullmatch(t):
            raise ValueError(f"time {t!r} is not YYYYMMDD HHMMSS")
        at = datetime.strptime(t, TIME_FORMAT).replace(tzinfo=UTC) + EST
        volume = Decimal(v)
        return M1Bar(at, Decimal(o), Decimal(h), Decimal(lo), Decimal(c),
                     None if volume == 0 else volume, spread=None)  # fmt: skip
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"HistData {period.tag} line {line}: {exc}") from exc
