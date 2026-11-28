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

No-trade minutes (`NO_TRADE_FILL`, pre-declared per source). A source such as FirstRate
leaves out minutes with no trade, so a missing minute there is not missing data. Between
two consecutive bars, the open-session minutes with no bar form one run (scheduled
closures do not count toward it). A run of at most the source's limit is filled: each
minute becomes a flat bar at the previous close with volume 0 and no spread, marked
`filled`, and a resampled bar made only of filled minutes carries `NO_TRADE`. So it is
neither a GAP nor a missing bar for labels. A longer run is a real gap. A run with no bar
after it in the range is not filled: nothing says trading resumed. So extending a range
that ended inside a run rebuilds a stored bar differently, and the store refuses it
(`CandleConflict`): import the longer range as a new dataset version. To fill the start
of the range exactly as a one-pass import would, the source is read from `MAX_CLOSED`
plus the limit before it; those earlier rows only seed the fill. The report lists the
length of every filled run (runs over the limit show up only as gaps). A source with no
entry is never filled.

Chunks: the source is read once, in order, and split into chunks aligned to H1 bars, one
transaction each, so no bar spans two chunks. A chunk's first M5 bar takes its gap state
from the last bar stored before the chunk. Re-running a chunk therefore rebuilds
identical bars, and the store treats them as no-ops. An interrupted import resumes at
the chunk holding the newest stored bar. Import a version's range in time order:
extending it backwards would change the gap state of a stored chunk, and the store
refuses that (`CandleConflict`).

`CsvM1Source` is the generic source. Its format is documented on the class so that any
vendor export (Dukascopy or another) can be converted to it. `FirstRateGcSource`
(`firstrate.py`) reads FirstRate's own format directly; its prices are futures trades, not
bid. Nothing here downloads.
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
from app.market_data.session import MAX_CLOSED, ScheduleAudit, SessionSchedule, audit
from app.market_data.sources import NO_TRADE_FILL, SourceRegistry
from app.market_data.store import CandleStore
from app.storage.db import transaction

__all__ = ["ChunkReport", "CsvM1Source", "ImportReport", "import_history", "open_text"]

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
    filled_runs: list[int] = field(default_factory=list)
    """Length in minutes of each no-trade run filled in the range (`NO_TRADE_FILL`)."""

    @property
    def filled_minutes(self) -> int:
        return sum(self.filled_runs)

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
    """Import [start, end) chunk by chunk, resuming after the newest stored bar. Both ends
    lie on H1 boundaries and `end` is not after the clock, so no stored bar is cut short."""
    if chunk <= timedelta(0) or chunk % H1 or floor_time(start, H1) != start:
        raise ValueError("chunks must start on and span whole H1 bars")
    if floor_time(end, H1) != end:
        raise ValueError("the range must end on an H1 bar boundary")
    if end > clock():
        raise ValueError("the range must end by the clock: only closed bars are imported")
    schedule = SourceRegistry(conn, clock=clock).register(dataset_version, data_source)
    store = CandleStore(conn, dataset_version=dataset_version)
    newest = store.latest_open(symbol, Timeframe.M5, as_of=end)
    first = start if newest is None or newest < start else start + (newest - start) // chunk * chunk
    report = ImportReport()
    fill = NO_TRADE_FILL.get(data_source)
    lead = timedelta(0) if fill is None else MAX_CLOSED + fill
    # One pass over the source, split at chunk ends: validated rows only move forward.
    rows = _validated(source.m1_bars(first - lead, end), clock(), report.issues, first)
    if fill is not None:
        rows = _no_trade_filled(rows, schedule, fill, first, report.filled_runs)
    rows = (b for b in rows if b.open_time >= first)  # earlier rows only seed the fill
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
    seed = prev[Timeframe.M5][-1].open_time if prev[Timeframe.M5] else None
    built = _resample(symbol, bars, schedule, seed=seed, end=end)
    for b in built:
        store.save(b)
    audits = {}
    for tf in Timeframe:
        mine = [b for b in built if b.timeframe is tf]
        gaps = audit(schedule, [*prev[tf], *mine]).unexpected_gaps  # across the chunk edge
        audits[tf] = replace(audit(schedule, mine), unexpected_gaps=gaps)
    return ChunkReport(start, end, audits)


def _validated(
    rows: Iterable[M1Bar], now: datetime, issues: Counter[Issue], count_from: datetime
) -> Iterator[M1Bar]:
    """Valid rows in order; dropped rows from `count_from` on are counted in `issues`."""
    last: datetime | None = None
    for row in rows:
        if row.open_time.tzinfo is None:
            raise ValueError("naive bar time: a historical source must give UTC times")
        bar = replace(row, open_time=row.open_time.astimezone(UTC))
        if not _valid(bar):
            issue = Issue.INVALID_QUOTE
        elif bar.open_time + M1 > now + CLOCK_SKEW:
            issue = Issue.FUTURE_TICK
        elif last is not None and bar.open_time < last:
            issue = Issue.OUT_OF_ORDER
        elif bar.open_time == last:
            issue = Issue.DUPLICATE
        else:
            last = bar.open_time
            yield bar
            continue
        if bar.open_time >= count_from:
            issues[issue] += 1


def _no_trade_filled(
    rows: Iterable[M1Bar],
    schedule: SessionSchedule,
    limit: timedelta,
    first: datetime,
    runs: list[int],
) -> Iterator[M1Bar]:
    """The rows, with each no-trade run of at most `limit` open minutes between two rows
    filled flat at the previous close. Runs reaching `first` or later are listed in `runs`."""
    prev: M1Bar | None = None
    for bar in rows:
        if prev is not None:
            run = _open_minutes(schedule, prev.open_time + M1, bar.open_time, limit // M1)
            if run and run[-1] >= first:
                runs.append(len(run))
            for t in run:
                yield M1Bar(t, prev.close, prev.close, prev.close, prev.close, Decimal(0),
                            spread=None, filled=True)  # fmt: skip
        yield bar
        prev = bar


def _open_minutes(
    schedule: SessionSchedule, lo: datetime, hi: datetime, most: int
) -> list[datetime]:
    """The open-session minutes in [lo, hi), or none if there are more than `most`."""
    out: list[datetime] = []
    t = lo
    while t < hi:
        if schedule.is_open(t):
            if len(out) == most:
                return []
            out.append(t)
        t += M1
    return out


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
    seed: datetime | None,
    end: datetime,
) -> list[CandleClosed]:
    """M5/M15/H1 bars from one chunk's validated M1 bars, with live GAP semantics. `seed`
    is the open time of the last M5 bar stored before the chunk."""
    present = {floor_time(b.open_time, M5) for b in bars}
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
    flags = [QualityFlag.GAP.value] if gap else []
    if all(b.filled for b in group):
        flags.append(QualityFlag.NO_TRADE.value)
    return CandleClosed(
        symbol=symbol, timeframe=tf, open_time=opened, close_time=opened + size,
        open=group[0].open, high=max(b.high for b in group), low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum(volumes, Decimal(0)) if len(volumes) == len(group) else None,
        spread=max(spreads, default=None), available_at=opened + size,
        quality_flags=flags,
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
        with open_text(self.path) as f:
            reader = csv.reader(f)
            if tuple(next(reader, ())) != self.HEADER:
                raise ValueError(f"{self.path}: header must be {','.join(self.HEADER)}")
            for line, row in enumerate(reader, start=2):
                bar = self._parse(row, line)
                if start <= bar.open_time < end:
                    yield bar

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


def open_text(path: Path) -> TextIO:
    """A UTF-8 text file, gunzipped on the fly if its name ends in `.gz`."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open(encoding="utf-8", newline="")
