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
    "day_url",
    "decode_candles",
]

BASE = "https://datafeed.dukascopy.com/datafeed"
PRICE_DIVISOR = 1000
"""XAUUSD prices are stored as integers of 1/1000 [PROPOSED: verify on a known day]."""
USER_AGENT = "Mozilla/5.0 (compatible; historical-data-client/1.0)"
RECORD = struct.Struct(">5if")
SATURDAY = 5

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
    return (
        f"{BASE}/{symbol}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"
    )


def decode_candles(raw: bytes, day: date, divisor: int = PRICE_DIVISOR) -> list[Candle]:
    if not raw:
        return []
    data = lzma.decompress(raw)
    if len(data) % RECORD.size:
        raise ValueError(f"{day}: {len(data)} bytes is not a whole number of 24-byte records")
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    scale = Decimal(divisor)
    out = []
    for s, o, c, lo, h, v in RECORD.iter_unpack(data):
        o_, c_, lo_, h_ = (Decimal(x) / scale for x in (o, c, lo, h))
        volume = Decimal(format(v, ".7g"))  # float32: keep its 7 significant digits
        out.append(Candle(start + timedelta(seconds=s), o_, c_, lo_, h_, volume))
    return out


def _urllib(url: str, user_agent: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
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
