"""Deterministic, leak-free dataset export for training M04 later (#110, PRD #107;
LRN-04, LRN-06; AT-15). Nothing here trains.

`export_dataset` turns the recorded bars of one candle dataset version into sorted JSONL,
one row per bar, and registers it as an M07 `DatasetManifest` (window, time-ordered
splits, SHA-256 of the exact bytes). Each row is point in time:
- features: the bar's recorded snapshot (as of the bar's close);
- labels: the bar's outcome per horizon, only if known by `window_end` (else null);
- news: the newest news assessment already available at the bar's close (`as_of`), and
  only if still live then; an expired newest one gives no news, never an older one (the
  same refusal-first rule as `journal_evidence`). Joining at the close, not at when the
  snapshot was taken, keeps news that arrived after the close (e.g. over a weekend) out
  of a row whose label is measured from that close.
- `labelled`: true only if every horizon has a raw `move` label (a trainer must not read
  a missing label as zero). `barrier_labelled` is the same for `barrier_labels`: a
  three-class trainer filters on it, never on `labelled`.
- `barrier_labels`: the bar's triple-barrier label per horizon (#129; `label_version`,
  UP/DOWN/FLAT, its bid move and the timeframe it was read on), measured from bars known
  by `window_end`, else null. It carries `ends_at`, the close of the label's last slot:
  a walk-forward fold must purge on `ends_at` (not on `as_of + horizon`), because a
  scheduled closure inside the window pushes it later. A row with an AMBIGUOUS
  (dual-touch) label is left out; the manifest's `label_stats` count them per era (each
  split, and "all"). The raw `move` labels are unchanged beside them.
Only one candle dataset version and one feature schema are exported. With `transforms`,
`features` are the M04 transforms of the recorded snapshot (`transform_from_store`, the
function the live recorder feeds a transform model with), under TRANSFORM_SCHEMA.

A row belongs to the split whose [start, end) holds its `as_of`. A row whose longest
label horizon would end after its split's end is purged (for a barrier label: the close
of its last slot, which a scheduled closure can push past the horizon's wall-clock end),
so no label is built from prices of the next split. With splits in time order (train,
validation, holdout, which the manifest enforces) this purge is enough; an embargo would
be needed only for k-fold or walk-forward schemes that train on data after the test
split. Rows outside every
split are left out.

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
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.core.contracts import FeatureSnapshot, NewsAssessment
from app.core.enums import Timeframe
from app.core.time import live
from app.learning.bars import BarLog, BarOutcome, Horizon, Provenance, SnapshotRow
from app.learning.labels import LABEL_VERSION, LabelStats, TripleBarrierLabel, label_at
from app.learning.lifecycle import RATE_Q, DatasetManifest, DatasetStore, LabelEraStats, Split
from app.market_data.features import ATR_M15, FEATURE_SCHEMA
from app.market_data.session import SessionSchedule
from app.market_data.store import CandleStore
from app.prediction.transforms import TRANSFORM_SCHEMA, transform_from_store
from app.storage.journal import Journal, payload_as

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


def _barrier(lab: TripleBarrierLabel | None) -> dict[str, object] | None:
    if lab is None:
        return None
    return {
        "label_version": lab.label_version,
        "outcome": lab.outcome.value,
        "distance": _num(lab.distance),
        "move": _num(lab.move),
        "timeframe": lab.timeframe.value,
        "available_at": lab.available_at.isoformat(),
        "ends_at": lab.ends_at.isoformat(),
    }


def _era(era: str, labels: list[TripleBarrierLabel | None]) -> LabelEraStats:
    s = LabelStats.of(labels)

    def q(x: Decimal | None) -> Decimal | None:
        return None if x is None else x.quantize(RATE_Q)

    return LabelEraStats(era=era, rows=s.rows, missing=s.missing,
                         dual_touch_count=s.ambiguous, dual_touch_rate=q(s.dual_touch_rate),
                         flat_share=q(s.flat_share))  # fmt: skip


def _row(
    snap: SnapshotRow,
    split: str,
    labels: dict[str, BarOutcome | None],
    barriers: dict[str, TripleBarrierLabel | None],
    news: NewsAssessment | None,
    features: FeatureSnapshot,
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
        "feature_schema": features.feature_schema,
        "quality": features.quality.value,
        "features": {k: _num(v) for k, v in sorted(features.values.items())},
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
        "barrier_labels": {k: _barrier(b) for k, b in sorted(barriers.items())},
        "barrier_labelled": all(b is not None for b in barriers.values()),
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
    feature_schema: str = FEATURE_SCHEMA,
    transforms: bool = False,
    session: SessionSchedule | None,
) -> DatasetManifest:
    """Write the dataset to `out`, register its manifest, and return it. `session` (no
    default, so a caller always chooses): the market's schedule, for the barrier labels'
    and the transforms' bar windows; the recorder that fed the models must use the same
    one. None means 24/7: for XAUUSD pass `XAUUSD_NY_1700`, or every scheduled closure
    is a gap that leaves rows unlabelled. The manifest records it as
    `session:<schedule_id|24x7>`."""
    hs = sorted(horizons, key=horizon_key)
    longest = max(h.length for h in hs)
    log = BarLog(conn)
    candles = CandleStore(conn, dataset_version=dataset_version)
    outcomes = {
        horizon_key(h): {(o.as_of, o.dataset_version): o
                         for o in log.outcomes(symbol, h, start=window_start, end=window_end)}
        for h in hs
    }  # fmt: skip
    news = _News(conn, before=window_end)
    lines: list[str] = []
    kept: dict[str, list[TripleBarrierLabel | None]] = {s.name: [] for s in splits}
    for snap in log.snapshots(symbol, timeframe, start=window_start, end=window_end):
        if (snap.dataset_version, snap.snapshot.feature_schema) != (dataset_version,
                                                                     feature_schema):  # fmt: skip
            continue
        if provenance is not None and snap.provenance not in provenance:
            continue
        as_of = snap.snapshot.as_of
        split = next((s for s in splits if s.start <= as_of < s.end), None)
        if split is None or as_of + longest > split.end:
            continue  # outside every split, or its label would reach into the next one
        atr = snap.snapshot.values.get(ATR_M15)
        barriers = {horizon_key(h): label_at(candles, symbol, as_of, atr, h, now=window_end,
                                             session=session)
                    for h in hs}  # fmt: skip
        if any(b is not None and b.ends_at > split.end for b in barriers.values()):
            continue  # a barrier window that a closure stretched into the next split
        kept[split.name].extend(barriers.values())
        if any(b is not None and not b.labelled for b in barriers.values()):
            continue  # a dual touch: no class label
        labels = {}
        for key, by_bar in outcomes.items():
            o = by_bar.get((as_of, dataset_version))
            labels[key] = o if o is not None and o.available_at <= window_end else None
        features = (transform_from_store(candles, snap.snapshot, session=session)
                    if transforms else snap.snapshot)  # fmt: skip
        row = _row(snap, split.name, labels, barriers, news.at(as_of), features)
        lines.append(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    tag = "all" if provenance is None else "+".join(sorted(p.value for p in provenance))
    manifest = DatasetManifest(
        dataset_id=dataset_id, version=version,
        source_refs=[f"learning_snapshots:{symbol}:{timeframe.value}:{dataset_version}:"
                     f"{feature_schema}:{tag}",
                     *(f"learning_outcomes:{horizon_key(h)}" for h in hs),
                     f"labels:{LABEL_VERSION}",
                     f"session:{'24x7' if session is None else session.schedule_id}",
                     *([f"transforms:{TRANSFORM_SCHEMA}"] if transforms else []),
                     "journal:news.assessment"],
        window_start=window_start, window_end=window_end, splits=splits,
        content_hash=f"sha256:{hashlib.sha256(data).hexdigest()}",
        label_stats=[_era("all", [lab for labs in kept.values() for lab in labs]),
                     *(_era(name, labs) for name, labs in kept.items())],
    )  # fmt: skip
    DatasetStore(conn).save(manifest)  # validated and registered before any write
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return manifest
