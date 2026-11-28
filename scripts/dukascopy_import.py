"""Fetch Dukascopy XAUUSD M1 candles into the local cache, then import them (#128).

Owner decision 2026-11-28: Dukascopy XAUUSD is the primary corpus. Two steps, so the slow,
polite download can run in any order while the import stays in time order:

    uv run python scripts/dukascopy_import.py fetch --start 2022-01-01 --end 2026-11-28
    uv run python scripts/dukascopy_import.py fetch --start 2008-01-01 --end 2009-01-01
    uv run python scripts/dukascopy_import.py import --db <scratch.db> --start 2008-01-01 \\
        --end 2026-11-28 --version duka-xau-v1

- `fetch` checks the price scale and the 0-based month on a reference day first, then
  downloads day files one at a time (`DukascopyFetcher`: pause, backoff, cache under
  `data/historical/dukascopy/`, gitignored). It stops if free disk would fall below
  `--min-free-gb`.
- `import` reads only the cache (no network), imports into the given SQLite file (never
  `data/orionfx.db` by default), and prints the report: rows, audit, filled runs, issues,
  timings, and the measured spread per era beside STRESSED_COST (information only).
"""

import argparse
import shutil
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.core.enums import Timeframe
from app.learning.cost_log import era_costs
from app.market_data.dukascopy import DukascopyFetcher, DukascopyM1Source, decode_candles
from app.market_data.history import import_history
from app.market_data.sources import SEALED_FROM
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "historical" / "dukascopy"
SOURCE = "dukascopy-xauusd"
LEAD = timedelta(days=5)
"""The importer reads MAX_CLOSED + the no-tick fill limit before a range: fetch that too."""
REFERENCE_DAY = date(2020, 8, 6)
REFERENCE_RANGE = (Decimal(1990), Decimal(2100))
"""Gold's record near $2,070 on 2020-08-06 [from memory, not a cited source]. The
neighbouring months (July ~$1,780, September ~$1,930) and a wrong divisor (x10) fall
outside it, so a pass checks both the price scale and the 0-based month."""
STRESSED_SPREAD, STRESSED_TOTAL = Decimal("0.80"), Decimal("1.07")
"""`STRESSED_COST` on the #129 branch (spread 0.80; spread + 2 slippage + commission 1.07)."""


def _day(text: str) -> date:
    return date.fromisoformat(text)


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def check_reference(fetcher: DukascopyFetcher) -> None:
    candles = decode_candles(fetcher.day_file("XAUUSD", "BID", REFERENCE_DAY), REFERENCE_DAY)
    closes = [c.close for c in candles if c.volume > 0]
    if not closes:
        sys.exit(f"reference day {REFERENCE_DAY}: no candles; scale and month unverified")
    lo, hi = min(closes), max(closes)
    print(f"reference {REFERENCE_DAY}: bid close range {lo}..{hi}")
    if not (REFERENCE_RANGE[0] <= lo and hi <= REFERENCE_RANGE[1]):
        sys.exit(f"reference day outside {REFERENCE_RANGE}: check PRICE_DIVISOR and the month")


def fetch(args: argparse.Namespace) -> None:
    fetcher = DukascopyFetcher(args.cache, interval=args.interval)
    check_reference(fetcher)
    day, t0 = args.start - LEAD, time.perf_counter()  # the importer reads LEAD back
    while day < args.end:
        free_gb = shutil.disk_usage(args.cache).free / 1e9
        if free_gb < args.min_free_gb:
            sys.exit(f"stopped at {day}: {free_gb:.1f} GB free < {args.min_free_gb} GB")
        if day.weekday() != 5:
            for side in ("BID", "ASK"):
                fetcher.day_file("XAUUSD", side, day)
        if day.day == 1:
            print(f"{day} requests={fetcher.requests} {time.perf_counter() - t0:.0f}s "
                  f"free={free_gb:.1f}GB", flush=True)  # fmt: skip
        day += timedelta(days=1)
    print(f"done {args.start}..{args.end} requests={fetcher.requests}")


def _offline(url: str, user_agent: str, timeout: float) -> tuple[int, bytes]:
    raise RuntimeError(f"import reads the cache only; {url} is not cached")


def run_import(args: argparse.Namespace) -> None:
    fetcher = DukascopyFetcher(args.cache, transport=_offline)
    source = DukascopyM1Source(fetcher)
    conn = connect(args.db)
    migrate(conn)
    t0 = time.perf_counter()
    report = import_history(conn, source, data_source=SOURCE, symbol="XAUUSD",
                            dataset_version=args.version, start=_utc(args.start),
                            end=_utc(args.end))  # fmt: skip
    elapsed = time.perf_counter() - t0
    print(f"import {args.start}..{args.end}: {elapsed:.1f}s, {len(report.chunks)} chunks")
    store = CandleStore(conn, dataset_version=args.version)
    for tf in Timeframe:
        n = conn.execute("SELECT COUNT(*) FROM candles WHERE dataset_version = ? AND"
                         " timeframe = ?", (args.version, tf.value)).fetchone()[0]  # fmt: skip
        print(f"  {tf.value}: rows={n} in_closed={report.in_closed(tf)}"
              f" unexpected_gaps={report.unexpected_gaps(tf)}")  # fmt: skip
    print(f"  filled runs={len(report.filled_runs)} minutes={report.filled_minutes}"
          f" issues={dict(report.issues)} fits={report.fits}")  # fmt: skip
    sealed = SEALED_FROM[SOURCE]
    m15 = store.bars("XAUUSD", Timeframe.M15, as_of=sealed, after=_utc(args.start) - timedelta(1))
    rows = era_costs(source.m1_bars(_utc(args.start), min(_utc(args.end), sealed)), m15,
                     sealed_from=sealed)  # fmt: skip
    print(f"spread per era (before {sealed.date()}), STRESSED_COST spread {STRESSED_SPREAD},"
          f" total {STRESSED_TOTAL}:")  # fmt: skip
    for r in rows:
        print(f"  {r.era}: minutes={r.minutes} median_spread={r.median_spread}"
              f" median_spread/ATR_M15={r.median_spread_over_atr}")  # fmt: skip


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    i = sub.add_parser("import")
    for s in (f, i):
        s.add_argument("--start", type=_day, required=True)
        s.add_argument("--end", type=_day, required=True)
        s.add_argument("--cache", type=Path, default=CACHE)
    f.add_argument("--interval", type=float, default=1.0)
    f.add_argument("--min-free-gb", type=float, default=3.0)
    i.add_argument("--db", type=Path, required=True)
    i.add_argument("--version", required=True)
    args = p.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    fetch(args) if args.cmd == "fetch" else run_import(args)


if __name__ == "__main__":
    main()
