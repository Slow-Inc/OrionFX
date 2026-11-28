"""Dukascopy XAUUSD M1 candles from the public datafeed (#128, PRD #120 slice 1; DAT-01).

Owner decision 2026-11-28: Dukascopy XAUUSD is the primary training corpus (free and
publicly distributed; `docs/research/2026-11-28-dukascopy-terms.md` keeps the terms as
history). Raw files are cached under the gitignored `data/historical/dukascopy/` and never
committed.

Format (the public datafeed, as decoded by the common open-source readers; **[PROPOSED]
until checked against a known day's price**, see `PRICE_DIVISOR`):
- One file per UTC day and side: `.../datafeed/XAUUSD/{year}/{month-1:02}/{day:02}/
  {BID|ASK}_candles_min_1.bi5`. The month in the path is **0-based**.
- The file is LZMA ("alone" format) compressed; empty means no data that day.
- Records are 24 bytes, big-endian: int32 seconds since the start of the UTC day, then
  int32 open, close, low, high in units of 1/`PRICE_DIVISOR`, then float32 volume.

The source (`DukascopyM1Source`):
- OHLC are **bid**, timestamps UTC, as live bars are built from bid ticks.
- `spread = ask_close - bid_close` per minute (the close-to-close spread, not the minute's
  widest or mean; the per-minute ask candle has no tick-level spread).
- A candle with zero volume had no tick (the feed can carry flat filler minutes), so it is
  dropped. The importer's no-tick rule (`NO_TRADE_FILL["dukascopy-xauusd"]`) then fills
  short runs flat. Volume is Dukascopy's tick volume, not traded volume.
- Saturdays (UTC) are skipped: XAUUSD is closed all day, so no request is made.

The fetcher (`DukascopyFetcher`) is polite by construction: one request at a time, a pause
of `interval` seconds between requests, a few attempts with doubling backoff, a generic
User-Agent with no personal data, and a local cache, so each file is requested once.
"""

import lzma
import re
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.market_data.port import M1Bar

__all__ = [
    "PRICE_DIVISOR",
    "USER_AGENT",
    "Candle",
    "DukascopyFetcher",
    "DukascopyM1Source",
    "FetchFailed",
    "check_day",
    "day_url",
    "decode_candles",
]

BASE = "https://datafeed.dukascopy.com/datafeed"
PRICE_DIVISOR = 1000
"""XAUUSD prices are stored as integers of 1/1000 [PROPOSED: verify on a known day]."""
USER_AGENT = "Mozilla/5.0 (compatible; historical-data-client/1.0)"
RECORD = struct.Struct(">5if")
MAX_RECORDS = 24 * 60
"""A day file holds at most one candle per minute."""
MAX_RAW = 256 * 1024
"""The largest compressed day file accepted (a full day is about 10-40 KB): remote bytes
are capped before and after decompression, so a hostile or broken file cannot exhaust
memory or disk."""
SATURDAY = 5
SYMBOL = re.compile(r"[A-Z0-9]{3,12}")
SIDES = ("BID", "ASK")

Transport = Callable[[str, str, float], tuple[int, bytes]]
"""(url, user_agent, timeout) -> (HTTP status, body)."""


class FetchFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: datetime
    open: Decimal
    close: Decimal
    low: Decimal
    high: Decimal
    volume: Decimal


def day_url(symbol: str, side: str, day: date) -> str:
    """The day file's URL; the symbol and side are checked, since they also name the
    cache path."""
    if not SYMBOL.fullmatch(symbol) or side not in SIDES:
        raise ValueError(f"bad symbol or side: {symbol!r} {side!r}")
    return (
        f"{BASE}/{symbol}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"
    )


def decode_candles(raw: bytes, day: date, divisor: int = PRICE_DIVISOR) -> list[Candle]:
    if not raw:
        return []
    if len(raw) > MAX_RAW:
        raise ValueError(f"{day}: {len(raw)} compressed bytes is over the {MAX_RAW} cap")
    decoder = lzma.LZMADecompressor()
    data = decoder.decompress(raw, max_length=MAX_RECORDS * RECORD.size + 1)
    if len(data) > MAX_RECORDS * RECORD.size:
        raise ValueError(f"{day}: more than {MAX_RECORDS} records")
    if not decoder.eof:
        raise ValueError(f"{day}: the compressed stream is truncated")
    if len(data) % RECORD.size:
        raise ValueError(f"{day}: {len(data)} bytes is not a whole number of 24-byte records")
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    scale = Decimal(divisor)
    out: list[Candle] = []
    for s, o, c, lo, h, v in RECORD.iter_unpack(data):
        if (
            not 0 <= s < 86400
            or s % 60
            or (out and start + timedelta(seconds=s) <= out[-1].open_time)
        ):
            raise ValueError(f"{day}: record time {s}s is off the minute grid or out of order")
        o_, c_, lo_, h_ = (Decimal(x) / scale for x in (o, c, lo, h))
        volume = Decimal(format(v, ".7g"))  # float32: keep its 7 significant digits
        out.append(Candle(start + timedelta(seconds=s), o_, c_, lo_, h_, volume))
    return out


def check_day(bid: list[Candle], ask: list[Candle], low: Decimal, high: Decimal) -> list[str]:
    """Problems on a reference day, before a long fetch trusts the format: every candle is
    well formed (low <= open, close <= high) and inside [low, high], and ask >= bid at each
    minute's close. An empty list means the day looks right."""
    problems = []
    for side, candles in (("bid", bid), ("ask", ask)):
        if not candles:
            problems.append(f"no {side} candles")
        for c in candles:
            if not (c.low <= min(c.open, c.close) and max(c.open, c.close) <= c.high):
                problems.append(f"{side} {c.open_time:%H:%M} is not low <= open, close <= high")
            if not (low <= c.low and c.high <= high):
                problems.append(f"{side} {c.open_time:%H:%M} lies outside {low}..{high}")
    asks = {c.open_time: c.close for c in ask}
    for c in bid:
        if c.open_time in asks and asks[c.open_time] < c.close:
            problems.append(f"{c.open_time:%H:%M} ask close is below bid close")
    return problems


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # a 3xx comes back as an HTTP error: only the fixed host is read


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib(url: str, user_agent: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_RAW + 1)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""  # a network error: retried like a 5xx


class DukascopyFetcher:
    """Day files, from the local cache or (rate-limited) from the datafeed."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        transport: Transport = _urllib,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 1.0,
        attempts: int = 4,
        backoff: float = 5.0,
        timeout: float = 60.0,
    ) -> None:
        self.cache_dir = cache_dir
        self._transport = transport
        self._sleep = sleep
        self._interval = interval
        self._attempts = attempts
        self._backoff = backoff
        self._timeout = timeout
        self._requested = False
        self.requests = 0

    def path(self, symbol: str, side: str, day: date) -> Path:
        day_url(symbol, side, day)  # checks the symbol and side that name the path
        return self.cache_dir / symbol / f"{day.year:04d}" / f"{day.isoformat()}_{side}.bi5"

    def day_file(self, symbol: str, side: str, day: date) -> bytes:
        path = self.path(symbol, side, day)
        if path.exists():
            return path.read_bytes()
        body = self._get(day_url(symbol, side, day))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        return body

    def _get(self, url: str) -> bytes:
        if self._requested:
            self._sleep(self._interval)
        self._requested = True
        status = 0
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            self.requests += 1
            status, body = self._transport(url, USER_AGENT, self._timeout)
            if status == 200:
                if len(body) > MAX_RAW:
                    raise FetchFailed(f"{url}: body over the {MAX_RAW}-byte cap")
                return body
            if status == 404:
                return b""  # no file for that day: cached as no data
        raise FetchFailed(f"{url}: HTTP {status} after {self._attempts} attempts")


class DukascopyM1Source:
    def __init__(self, fetcher: DukascopyFetcher, symbol: str = "XAUUSD") -> None:
        self._fetcher = fetcher
        self._symbol = symbol

    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        day = start.astimezone(UTC).date()
        last = (end.astimezone(UTC) - timedelta(microseconds=1)).date()
        while day <= last:
            if day.weekday() != SATURDAY:
                yield from (b for b in self._day(day) if start <= b.open_time < end)
            day += timedelta(days=1)

    def _day(self, day: date) -> Iterator[M1Bar]:
        bid = decode_candles(self._fetcher.day_file(self._symbol, "BID", day), day)
        ask = {c.open_time: c for c in
               decode_candles(self._fetcher.day_file(self._symbol, "ASK", day), day)}  # fmt: skip
        for c in bid:
            if c.volume <= 0:
                continue  # no tick in that minute
            a = ask.get(c.open_time)
            yield M1Bar(c.open_time, c.open, c.high, c.low, c.close, c.volume,
                        spread=None if a is None else a.close - c.close)  # fmt: skip
