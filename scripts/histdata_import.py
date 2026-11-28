"""Fetch HistData XAUUSD M1 zips into the local cache, then import them (#128).

Owner decision 2026-11-28: HistData is the bulk M1 corpus (free public data). Two steps:

    uv run python scripts/histdata_import.py fetch --start-year 2009 --end-year 2026 \\
        --cache data/historical/histdata
    uv run python scripts/histdata_import.py import --db <scratch.db> --version hd-xau-v1 \\
        --cache data/historical/histdata

- `fetch` downloads one period at a time with at least 3 s between requests: a year file
  for each past year, and month files for the current year up to the current month (a
  month not published yet is skipped). It stops if free disk falls below `--min-free-gb`.
- `import` reads only the cache (no network), imports every cached period into the given
  SQLite file (never `data/orionfx.db` by default) as data source `histdata-xauusd`, and
  prints rows, the audit against XAUUSD_NY_1700, filled runs and dropped rows. The import
  stores everything; the seal (`SEALED_FROM`) limits what may be read afterwards.
"""

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION, utc_now
from app.market_data.candles import floor_time
from app.market_data.histdata import (
    MIN_INTERVAL,
    FetchFailed,
    HistDataFetcher,
    HistDataM1Source,
    Period,
)
from app.market_data.history import import_history
from app.storage.db import connect, migrate

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "historical" / "histdata"
SOURCE = "histdata-xauusd"


def periods(start_year: int, end_year: int, today: datetime) -> list[Period]:
    """Year files for past years; month files (to this month) for the current year."""
    out = []
    for y in range(start_year, end_year + 1):
        if y < today.year:
            out.append(Period(y))
        elif y == today.year:
            out.extend(Period(y, m) for m in range(1, today.month + 1))
    return out


def fetch(args: argparse.Namespace) -> None:
    fetcher = HistDataFetcher(args.cache, interval=args.interval)
    t0 = time.perf_counter()
    for p in periods(args.start_year, args.end_year, utc_now()):
        free_gb = shutil.disk_usage(args.cache).free / 1e9
        if free_gb < args.min_free_gb:
            sys.exit(f"stopped before {p.tag}: {free_gb:.1f} GB free < {args.min_free_gb} GB")
        try:
            size = len(fetcher.fetch(p))
        except FetchFailed as exc:
            if p.month is None:
                raise
            print(f"{p.tag}: skipped ({exc})", flush=True)  # a month not published yet
            continue
        print(f"{p.tag}: {size} bytes, requests={fetcher.requests},"
              f" {time.perf_counter() - t0:.0f}s", flush=True)  # fmt: skip


def run_import(args: argparse.Namespace) -> None:
    source = HistDataM1Source(args.cache)
    found = source.periods()
    if not found:
        sys.exit(f"no cached HistData zips under {args.cache}")
    start = found[0].utc_span()[0]
    end = min(found[-1].utc_span()[1], floor_time(utc_now(), TIMEFRAME_DURATION[Timeframe.H1]))
    conn: sqlite3.Connection = connect(args.db)
    migrate(conn)
    t0 = time.perf_counter()
    report = import_history(conn, source, data_source=SOURCE, symbol="XAUUSD",
                            dataset_version=args.version, start=start, end=end)  # fmt: skip
    print(f"import {start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M} UTC from {len(found)} files:"
          f" {time.perf_counter() - t0:.1f}s, {len(report.chunks)} chunks")  # fmt: skip
    for tf in Timeframe:
        n = conn.execute("SELECT COUNT(*) FROM candles WHERE dataset_version = ? AND"
                         " timeframe = ?", (args.version, tf.value)).fetchone()[0]  # fmt: skip
        print(f"  {tf.value}: rows={n} in_closed={report.in_closed(tf)}"
              f" unexpected_gaps={report.unexpected_gaps(tf)}")  # fmt: skip
    print(f"  filled runs={len(report.filled_runs)} minutes={report.filled_minutes}"
          f" issues={dict(report.issues)} fits={report.fits}")  # fmt: skip


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--start-year", type=int, required=True)
    f.add_argument("--end-year", type=int, required=True)
    f.add_argument("--interval", type=float, default=MIN_INTERVAL)
    f.add_argument("--min-free-gb", type=float, default=3.0)
    i = sub.add_parser("import")
    i.add_argument("--db", type=Path, required=True)
    i.add_argument("--version", required=True)
    for s in (f, i):
        s.add_argument("--cache", type=Path, default=CACHE)
    args = p.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    if args.cmd == "fetch":
        fetch(args)
    else:
        run_import(args)


if __name__ == "__main__":
    main()
