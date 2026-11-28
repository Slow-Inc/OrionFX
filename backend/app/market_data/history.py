"""Historical M1 bar import (#128, PRD #120 slice 1; DAT-01, DAT-04, AT-15).

`import_history` reads M1 bars from a `HistoricalBarSource`, validates them as live data
is validated, resamples them to M5/M15/H1 and stores them in a `CandleStore` version
bound to one data source (`SourceRegistry`).

Validation, with the same rules and `Issue` names as `CandleEngine`. Bad rows are dropped
and counted, never folded into a bar:
- a crossed or non-positive bar, a negative spread or volume, or a time off the minute
  grid is INVALID_QUOTE;
- a bar not yet closed by the clock is FUTURE_TICK;
- a bar earlier than the previous one is OUT_OF_ORDER;
- a second bar for the same minute is DUPLICATE (the first wins).

Resampling and gaps:
- Bars are bid OHLC on UTC boundaries. Volume is the summed tick volume, spread is the
  widest, and `available_at` is the bar's close: a historical bar was available when it
  closed, and the import time never enters a bar.
- A gap is an M5 slot the data source's session schedule expects and no M1 bar fills.
  It flags the M5 bar right after it and every M15/H1 bar containing it, as live does.
  Scheduled closures are never gaps. Missing minutes inside an M5 slot are not a gap
  (live cannot see them either).
- The schedule comes from the data source. `audit` checks it against each chunk on
  import: bars inside a closed period (the schedule does not fit the source) and
  unexpected gaps are counted in the report. The bars are stored either way.

Chunks: the source is read once, in order, and split into chunks aligned to H1 bars, one
transaction each, so no bar spans two chunks. A chunk's first M5 bar takes its gap state
from the last bar stored before the chunk. Re-running a chunk therefore rebuilds
identical bars, and the store treats them as no-ops. An interrupted import resumes at
the chunk holding the newest stored bar. Import a version's range in time order:
extending it backwards would change the gap state of a stored chunk, and the store
refuses that (`CandleConflict`).

`CsvM1Source` is the first concrete source. Its format is documented on the class so that
any vendor export (Dukascopy or another) can be converted to it. Nothing here downloads.
"""

import bisect
import csv
import gzip
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from app.core.contracts import CandleClosed
from app.core.enums import QualityFlag, Timeframe
from app.core.time import CLOCK_SKEW, TIMEFRAME_DURATION, Clock, utc_now
from app.market_data.candles import M5, Issue, floor_time
from app.market_data.port import HistoricalBarSource, M1Bar
from app.market_data.session import ScheduleAudit, SessionSchedule, audit
from app.market_data.sources import SourceRegistry
from app.market_data.store import CandleStore
from app.storage.db import transaction

__all__ = ["ChunkReport", "CsvM1Source", "ImportReport", "import_history"]

M1 = timedelta(minutes=1)
H1 = TIMEFRAME_DURATION[Timeframe.H1]


@dataclass(frozen=True, slots=True)
class ChunkReport:
    start: datetime
    end: datetime
    audits: dict[Timeframe, ScheduleAudit]


@dataclass(frozen=True, slots=True)
class ImportReport:
    chunks: list[ChunkReport] = field(default_factory=list)
    issues: Counter[Issue] = field(default_factory=Counter)
    """Rows dropped by validation, over the whole run."""

    def in_closed(self, tf: Timeframe) -> int:
        """Bars inside a scheduled closure: the source's schedule does not fit its data."""
        return sum(c.audits[tf].in_closed for c in self.chunks)

    def unexpected_gaps(self, tf: Timeframe) -> int:
        """Expected slots with no bar: real missing data."""
        return sum(c.audits[tf].unexpected_gaps for c in self.chunks)

    @property
    def fits(self) -> bool:
        return all(self.in_closed(tf) == 0 for tf in Timeframe)


def import_history(
    conn: sqlite3.Connection,
    source: HistoricalBarSource,
    *,
    data_source: str,
    symbol: str,
    dataset_version: str,
    start: datetime,
    end: datetime,
    chunk: timedelta = timedelta(days=1),
    clock: Clock = utc_now,
) -> ImportReport:
    """Import [start, end) chunk by chunk, resuming after the newest stored bar."""
    if chunk <= timedelta(0) or chunk % H1 or floor_time(start, H1) != start:
        raise ValueError("chunks must start on and span whole H1 bars")
    schedule = SourceRegistry(conn, clock=clock).register(dataset_version, data_source)
    store = CandleStore(conn, dataset_version=dataset_version)
    newest = store.latest_open(symbol, Timeframe.M5, as_of=end)
    first = start if newest is None or newest < start else start + (newest - start) // chunk * chunk
    report = ImportReport()
    # One pass over the source, split at chunk ends: validated rows only move forward.
    rows = _validated(source.m1_bars(first, end), clock(), report.issues)
    pending = next(rows, None)
    t = first
    while t < end:
        c_end = min(t + chunk, end)
        bars = []
        while pending is not None and pending.open_time < c_end:
            bars.append(pending)
            pending = next(rows, None)
        with transaction(conn):
            report.chunks.append(_import_chunk(store, schedule, symbol, bars, t, c_end))
        t = c_end
    return report


def _import_chunk(
    store: CandleStore,
    schedule: SessionSchedule,
    symbol: str,
    bars: list[M1Bar],
    start: datetime,
    end: datetime,
) -> ChunkReport:
    prev = {tf: store.bars(symbol, tf, as_of=start, limit=1) for tf in Timeframe}
    built = _resample(symbol, bars, schedule, prev=prev[Timeframe.M5], end=end)
    for b in built:
        store.save(b)
    audits = {}
    for tf in Timeframe:
        mine = [b for b in built if b.timeframe is tf]
        gaps = audit(schedule, [*prev[tf], *mine]).unexpected_gaps  # across the chunk edge
        audits[tf] = replace(audit(schedule, mine), unexpected_gaps=gaps)
    return ChunkReport(start, end, audits)


def _validated(rows: Iterable[M1Bar], now: datetime, issues: Counter[Issue]) -> Iterator[M1Bar]:
    last: datetime | None = None
    for row in rows:
        if row.open_time.tzinfo is None:
            raise ValueError("naive bar time: a historical source must give UTC times")
        bar = replace(row, open_time=row.open_time.astimezone(UTC))
        if not _valid(bar):
            issues[Issue.INVALID_QUOTE] += 1
        elif bar.open_time + M1 > now + CLOCK_SKEW:
            issues[Issue.FUTURE_TICK] += 1
        elif last is not None and bar.open_time < last:
            issues[Issue.OUT_OF_ORDER] += 1
        elif bar.open_time == last:
            issues[Issue.DUPLICATE] += 1
        else:
            last = bar.open_time
            yield bar


def _valid(b: M1Bar) -> bool:
    prices = (b.open, b.high, b.low, b.close)
    extras = [x for x in (b.volume, b.spread) if x is not None]
    return (
        all(p.is_finite() and p > 0 for p in prices)
        and all(x.is_finite() and x >= 0 for x in extras)
        and b.low <= min(b.open, b.close)
        and max(b.open, b.close) <= b.high
        and floor_time(b.open_time, M1) == b.open_time
    )


def _resample(
    symbol: str,
    bars: list[M1Bar],
    schedule: SessionSchedule,
    *,
    prev: list[CandleClosed],
    end: datetime,
) -> list[CandleClosed]:
    """M5/M15/H1 bars from one chunk's validated M1 bars, with live GAP semantics."""
    present = {floor_time(b.open_time, M5) for b in bars}
    seed = prev[-1].open_time if prev else None
    first = seed if seed is not None else min(present, default=None)
    missed = _missed_slots(schedule, present, first, end)
    out: list[CandleClosed] = []
    for tf, size in TIMEFRAME_DURATION.items():
        groups: dict[datetime, list[M1Bar]] = {}
        for b in bars:
            groups.setdefault(floor_time(b.open_time, size), []).append(b)
        before = seed  # the previous M5 bar, for the bar after a hole
        for opened, group in groups.items():
            if size == M5:
                gap = before is not None and _any_in(missed, before + M5, opened)
                before = opened
            else:
                gap = _any_in(missed, opened, opened + size)
            out.append(_bar(symbol, tf, opened, size, group, gap))
    return out


def _missed_slots(
    schedule: SessionSchedule, present: set[datetime], first: datetime | None, end: datetime
) -> list[datetime]:
    """Expected M5 slots after `first` and before `end` with no bar, in time order."""
    if first is None:
        return []
    missed = []
    t = schedule.next_open_slot(first, M5)
    while t < end:
        if t not in present:
            missed.append(t)
        t = schedule.next_open_slot(t, M5)
    return missed


def _any_in(sorted_times: list[datetime], lo: datetime, hi: datetime) -> bool:
    """Whether any of the sorted times lies in [lo, hi)."""
    i = bisect.bisect_left(sorted_times, lo)
    return i < len(sorted_times) and sorted_times[i] < hi


def _bar(
    symbol: str, tf: Timeframe, opened: datetime, size: timedelta, group: list[M1Bar], gap: bool
) -> CandleClosed:
    volumes = [b.volume for b in group if b.volume is not None]
    spreads = [b.spread for b in group if b.spread is not None]
    return CandleClosed(
        symbol=symbol, timeframe=tf, open_time=opened, close_time=opened + size,
        open=group[0].open, high=max(b.high for b in group), low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum(volumes, Decimal(0)) if len(volumes) == len(group) else None,
        spread=max(spreads, default=None), available_at=opened + size,
        quality_flags=[QualityFlag.GAP.value] if gap else [],
    )  # fmt: skip


class CsvM1Source:
    """M1 bars from a CSV file (optionally gzip-compressed, `.gz`). The format, version 1:

    - UTF-8, comma-separated, with exactly this header line:
      `open_time,open,high,low,close,volume,spread`
    - `open_time`: the minute the bar opens, ISO 8601 **with a UTC offset**
      (`2026-01-07T20:00:00Z` or `...+02:00`). A naive time is refused: its zone is unknown.
    - `open,high,low,close`: **bid** prices as decimals.
    - `volume`: tick volume (an integer or decimal), or empty if the vendor has none.
    - `spread`: ask minus bid in price units (the minute's widest if known), or empty.
    - One row per minute that traded. Minutes with no trading may be left out.

    A Dukascopy (or any vendor's) export converts to this by writing bid OHLC per minute
    and, from the matching ask series, `spread = ask_close - bid_close` (or the minute's
    widest spread if the vendor gives ticks). A malformed row refuses the whole file: the
    converter is wrong, not the market. Parquet is not read yet (it would add pyarrow).
    """

    HEADER = ("open_time", "open", "high", "low", "close", "volume", "spread")

    def __init__(self, path: Path) -> None:
        self.path = path

    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        with self._open() as f:
            reader = csv.reader(f)
            if tuple(next(reader, ())) != self.HEADER:
                raise ValueError(f"{self.path}: header must be {','.join(self.HEADER)}")
            for line, row in enumerate(reader, start=2):
                bar = self._parse(row, line)
                if start <= bar.open_time < end:
                    yield bar

    def _open(self) -> TextIO:
        if self.path.suffix == ".gz":
            return gzip.open(self.path, "rt", encoding="utf-8", newline="")
        return self.path.open(encoding="utf-8", newline="")

    def _parse(self, row: list[str], line: int) -> M1Bar:
        try:
            t, o, h, lo, c, v, s = row
            at = datetime.fromisoformat(t)
            if at.tzinfo is None:
                raise ValueError("time has no UTC offset")
            return M1Bar(at.astimezone(UTC), Decimal(o), Decimal(h), Decimal(lo), Decimal(c),
                         Decimal(v) if v else None, Decimal(s) if s else None)  # fmt: skip
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"{self.path} line {line}: {exc} (UTC offset required)") from exc
