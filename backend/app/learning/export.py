"""Deterministic, leak-free dataset export for training M04 (#110, #128; PRD #107, #120;
LRN-04, LRN-06, VAL-02; AT-15). Nothing here trains.

`export_dataset` turns the recorded bars of one or more candle dataset versions into
sorted JSONL, one row per bar, and registers it as an M07 `DatasetManifest` (window, fold
plan, data sources, SHA-256 of the exact bytes). Each row is point in time:
- features: the bar's recorded snapshot (as of the bar's close);
- labels: the bar's outcome per horizon, only if known by `window_end` (else null);
- news: the newest news assessment already available at the bar's close (`as_of`), and
  only if still live then; an expired newest one gives no news, never an older one (the
  same refusal-first rule as `journal_evidence`). Joining at the close, not at when the
  snapshot was taken, keeps news that arrived after the close (e.g. over a weekend) out
  of a row whose label is measured from that close.
- `labelled`: true only if every horizon has a label (a trainer must not read a missing
  label as zero).
- `data_source`: the feed the bar came from (`SourceRegistry`). A dataset version with
  no registered source is refused.
Only one feature schema is exported.

Folds come from X02 `walk_forward` over [window_start, window_end). The last `holdout` is
sealed, and each fold trains, waits an embargo, then tests. A row belongs to a window (a
fold's train or test, or the holdout) only if `start <= as_of`, its longest label ends by
the window's end (`as_of + longest <= end`: the purge by label end), and its snapshot was
available by then (`available_at <= end`). Each row is written once, with the folds it
trains and tests in. Development rows in no fold (embargo gaps, purged edges) are left
out. The PR #147 review constraints hold:
- (a) every fold, train and test, is purged by label end, and the embargo must also be
  at least the longest horizon;
- (b) labels are wall-clock horizons, so `as_of + longest` is the end bar's close. A
  session-aware horizon that counts bars across a break must purge on that bar's real
  close time instead;
- (c) no fold trains on or after its test (the manifest refuses such a plan);
- (d) holdout rows are purged too, and the embargo separates the last test window from
  the holdout.
One fold plan on one time axis serves every data source, so each test (validation)
window comes after the training data of every source.

The manifest is validated and registered before any byte is written, and the file is
written to a temporary name and moved into place, so a refused export never overwrites a
registered file. The database is append-only, so re-exporting an old window can differ
if rows were added since; the content hash detects that and `DatasetStore` refuses to
reuse a (dataset_id, version) for different content.
"""

import bisect
import hashlib
import json
import os
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from app.core.contracts import FeatureSnapshot, NewsAssessment
from app.core.enums import Timeframe
from app.core.time import live
from app.learning.bars import BarLog, BarOutcome, Horizon, Provenance, SnapshotRow
from app.learning.lifecycle import DatasetManifest, DatasetStore, FoldWindow, Split
from app.market_data.features import FEATURE_SCHEMA
from app.market_data.sources import SourceRegistry, UnknownSource
from app.storage.journal import Journal, payload_as
from app.validation.splits import WalkForward, walk_forward

__all__ = ["export_dataset", "horizon_key"]


def horizon_key(h: Horizon) -> str:
    return f"{h.timeframe.value}x{h.bars}"


def _num(v: object) -> str | None:
    return None if v is None else str(v)


class _News:
    """Point-in-time lookup over every journaled news assessment."""

    def __init__(self, conn: sqlite3.Connection, *, before: datetime) -> None:
        items = [payload_as(e, NewsAssessment)
                 for e in Journal(conn).recent(["news.assessment"], limit=-1)]  # fmt: skip
        self._items = sorted((a for a in items if a.available_at < before),
                             key=lambda a: (a.available_at, a.assessment_id))  # fmt: skip
        self._times = [a.available_at for a in self._items]

    def at(self, t: datetime) -> NewsAssessment | None:
        i = bisect.bisect_right(self._times, t)
        if i == 0:
            return None
        newest = self._items[i - 1]
        return newest if live(newest.available_at, newest.valid_until, t) else None


def _row(
    snap: SnapshotRow,
    split: str,
    labels: dict[str, BarOutcome | None],
    news: NewsAssessment | None,
) -> dict[str, object]:
    s = snap.snapshot
    return {
        "split": split,
        "symbol": s.symbol,
        "timeframe": snap.timeframe.value,
        "as_of": s.as_of.isoformat(),
        "available_at": s.available_at.isoformat(),
        "provenance": snap.provenance.value,
        "mode": None if snap.mode is None else snap.mode.value,
        "dataset_version": snap.dataset_version,
        "feature_schema": s.feature_schema,
        "quality": s.quality.value,
        "features": {k: _num(v) for k, v in sorted(s.values.items())},
        "labelled": all(o is not None for o in labels.values()),
        "labels": {
            k: None
            if o is None
            else {
                "move": _num(o.move),
                "spread": _num(o.spread),
                "available_at": o.available_at.isoformat(),
            }
            for k, o in sorted(labels.items())
        },
        "news": None
        if news is None
        else {
            "assessment_id": news.assessment_id,
            "direction": news.direction.value,
            "impact": _num(news.impact),
            "score": _num(news.score.value),
            "available_at": news.available_at.isoformat(),
        },
    }


def export_dataset(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timeframe: Timeframe,
    horizons: Iterable[Horizon],
    dataset_versions: Collection[str],
    window_start: datetime,
    window_end: datetime,
    train: timedelta,
    test: timedelta,
    embargo: timedelta,
    holdout: timedelta,
    dataset_id: str,
    version: str,
    out: Path,
    provenance: set[Provenance] | None = None,
    feature_schema: str = FEATURE_SCHEMA,
) -> DatasetManifest:
    """Write the dataset to `out`, register its manifest, and return it."""
    hs = sorted(horizons, key=horizon_key)
    longest = max(h.length for h in hs)
    if embargo < longest:
        raise ValueError(f"embargo {embargo} is shorter than the longest horizon {longest}")
    if holdout <= timedelta(0):
        raise ValueError("a dataset needs a sealed holdout")
    registry = SourceRegistry(conn)
    sources: dict[str, str] = {}
    for v in sorted(dataset_versions):
        source = registry.source_of(v)
        if source is None:
            raise UnknownSource(f"candle dataset {v!r} has no data source")
        sources[v] = source
    plan = walk_forward(window_start, window_end, train=train, test=test, embargo=embargo,
                        holdout=holdout)  # fmt: skip
    log = BarLog(conn)
    outcomes = {
        horizon_key(h): {(o.as_of, o.dataset_version): o
                         for o in log.outcomes(symbol, h, start=window_start, end=window_end)}
        for h in hs
    }  # fmt: skip
    news = _News(conn, before=window_end)
    lines: list[str] = []
    for snap in log.snapshots(symbol, timeframe, start=window_start, end=window_end):
        s = snap.snapshot
        if snap.dataset_version not in sources or s.feature_schema != feature_schema:
            continue
        if provenance is not None and snap.provenance not in provenance:
            continue
        member = _membership(s, plan, longest)
        if member is None:
            continue  # purged, in an embargo gap, or outside every window
        split, folds = member
        labels = {}
        for key, by_bar in outcomes.items():
            o = by_bar.get((s.as_of, snap.dataset_version))
            labels[key] = o if o is not None and o.available_at <= window_end else None
        row = _row(snap, split, labels, news.at(s.as_of))
        row["folds"] = folds
        row["data_source"] = sources[snap.dataset_version]
        lines.append(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    tag = "all" if provenance is None else "+".join(sorted(p.value for p in provenance))
    manifest = DatasetManifest(
        dataset_id=dataset_id, version=version,
        source_refs=[*(f"learning_snapshots:{symbol}:{timeframe.value}:{v}:{feature_schema}:{tag}"
                       for v in sources),
                     *(f"learning_outcomes:{horizon_key(h)}" for h in hs),
                     "journal:news.assessment"],
        window_start=window_start, window_end=window_end,
        splits=[Split(name="development", start=window_start, end=plan.holdout.start),
                Split(name="holdout", start=plan.holdout.start, end=plan.holdout.end)],
        folds=[FoldWindow(**asdict(f)) for f in plan.folds],
        embargo=embargo, data_sources=sources,
        content_hash=f"sha256:{hashlib.sha256(data).hexdigest()}",
    )  # fmt: skip
    DatasetStore(conn).save(manifest)  # validated and registered before any write
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return manifest


def _membership(
    s: FeatureSnapshot, plan: WalkForward, longest: timedelta
) -> tuple[str, dict[str, list[int]]] | None:
    """The row's split and the folds it trains and tests in (none for a holdout row), or
    None if it belongs to no window. A row is inside [start, end) only if its longest label ends and
    its snapshot was available by `end`: the purge by label end, per window."""

    def inside(start: datetime, end: datetime) -> bool:
        return start <= s.as_of and s.as_of + longest <= end and s.available_at <= end

    if inside(plan.holdout.start, plan.holdout.end):
        return "holdout", {"train": [], "test": []}
    folds = {
        "train": [k for k, f in enumerate(plan.folds) if inside(f.train_start, f.train_end)],
        "test": [k for k, f in enumerate(plan.folds) if inside(f.test_start, f.test_end)],
    }
    return ("development", folds) if folds["train"] or folds["test"] else None
