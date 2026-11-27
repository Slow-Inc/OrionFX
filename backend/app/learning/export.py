"""Deterministic, leak-free dataset export for training M04 later (#110, PRD #107;
LRN-04, LRN-06; AT-15). Nothing here trains.

`export_dataset` turns the recorded bars of one candle dataset version into sorted JSONL,
one row per bar, and registers it as an M07 `DatasetManifest` (window, time-ordered
splits, SHA-256 of the exact bytes). Each row is point in time:
- features: the bar's recorded snapshot (as of the bar's close);
- labels: the bar's outcome per horizon, only if known by `window_end` (else null);
- news: the newest news assessment already available when the features were, and only
  if still live then; an expired newest one gives no news, never an older one (the same
  refusal-first rule as `journal_evidence`).
A row belongs to the split whose [start, end) holds its `as_of`. A row whose longest
label horizon would end after its split's end is purged, so no label is built from
prices of the next split. Rows outside every split are left out. Exporting the same
inputs twice gives byte-identical output; reusing a (dataset_id, version) for different
content is refused by `DatasetStore`.
"""

import bisect
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from app.core.contracts import NewsAssessment
from app.core.enums import Timeframe
from app.core.time import live
from app.learning.bars import BarLog, BarOutcome, Horizon, Provenance, SnapshotRow
from app.learning.lifecycle import DatasetManifest, DatasetStore, Split
from app.storage.journal import Journal, payload_as

__all__ = ["export_dataset", "horizon_key"]


def horizon_key(h: Horizon) -> str:
    return f"{h.timeframe.value}x{h.bars}"


def _num(v: object) -> str | None:
    return None if v is None else str(v)


class _News:
    """Point-in-time lookup over every journaled news assessment."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        items = [payload_as(e, NewsAssessment)
                 for e in Journal(conn).recent(["news.assessment"], limit=-1)]  # fmt: skip
        self._items = sorted(items, key=lambda a: (a.available_at, a.assessment_id))
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
    dataset_version: str,
    window_start: datetime,
    window_end: datetime,
    splits: list[Split],
    dataset_id: str,
    version: str,
    out: Path,
    provenance: set[Provenance] | None = None,
) -> DatasetManifest:
    """Write the dataset to `out`, register its manifest, and return it."""
    hs = sorted(horizons, key=horizon_key)
    longest = max(h.length for h in hs)
    log = BarLog(conn)
    outcomes = {
        horizon_key(h): {(o.as_of, o.dataset_version): o
                         for o in log.outcomes(symbol, h, start=window_start, end=window_end)}
        for h in hs
    }  # fmt: skip
    news = _News(conn)
    lines: list[str] = []
    for snap in log.snapshots(symbol, timeframe, start=window_start, end=window_end):
        if snap.dataset_version != dataset_version:
            continue
        if provenance is not None and snap.provenance not in provenance:
            continue
        as_of = snap.snapshot.as_of
        split = next((s for s in splits if s.start <= as_of < s.end), None)
        if split is None or as_of + longest > split.end:
            continue  # outside every split, or its label would reach into the next one
        labels = {}
        for key, by_bar in outcomes.items():
            o = by_bar.get((as_of, dataset_version))
            labels[key] = o if o is not None and o.available_at <= window_end else None
        row = _row(snap, split.name, labels, news.at(snap.snapshot.available_at))
        lines.append(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    out.write_bytes(data)
    tag = "all" if provenance is None else "+".join(sorted(p.value for p in provenance))
    manifest = DatasetManifest(
        dataset_id=dataset_id, version=version,
        source_refs=[f"learning_snapshots:{symbol}:{timeframe.value}:{dataset_version}:{tag}",
                     *(f"learning_outcomes:{horizon_key(h)}" for h in hs),
                     "journal:news.assessment"],
        window_start=window_start, window_end=window_end, splits=splits,
        content_hash=f"sha256:{hashlib.sha256(data).hexdigest()}",
    )  # fmt: skip
    DatasetStore(conn).save(manifest)
    return manifest
