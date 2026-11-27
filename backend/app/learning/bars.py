"""Per-bar dataset records for training M04 later (#108, PRD #107; LRN-02, LRN-06; AT-15).

Nothing here trains, and nothing here feeds trading: these rows are learning data only.

- A snapshot row is the feature snapshot of one closed bar, built from exactly the bars
  available at that bar's close, tagged with its provenance (LIVE while the bot runs,
  BACKFILL over historical candles), the account mode for LIVE rows, the candle dataset
  version and the feature schema. One row per (symbol, timeframe, as_of, schema,
  dataset version); the first writer wins.
- An outcome row is the raw close-to-close move over a horizon from the bar closing at
  `as_of` to the bar closing `horizon_bars` later (the same rule as M07's MARKET_MOVE
  experience labels). It is stamped with the end bar's `available_at`, the moment the
  move became known, and carries the start bar's spread when known. A missing start or
  end bar gives no label, never a stretched one (such rows, mostly next to weekends, are
  re-checked each pass; there are only a few per week). No neutral threshold is applied:
  that is a training choice.
Both tables are append-only (migration 0011). The first writer of a bar wins: if a
LIVE and a BACKFILL recorder both reach it, its provenance is whichever wrote first.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.contracts import ContractModel, FeatureSnapshot
from app.core.enums import Mode, Timeframe
from app.core.time import TIMEFRAME_DURATION, Clock, utc_now
from app.core.types import Dec, UtcDatetime
from app.market_data.store import CandleStore
from app.storage.db import transaction

__all__ = ["BarLog", "BarOutcome", "Horizon", "Provenance", "SnapshotRow"]


class Provenance(StrEnum):
    LIVE = "LIVE"
    BACKFILL = "BACKFILL"


@dataclass(frozen=True, slots=True)
class Horizon:
    timeframe: Timeframe
    bars: int

    @property
    def length(self) -> timedelta:
        return TIMEFRAME_DURATION[self.timeframe] * self.bars


class BarOutcome(ContractModel):
    symbol: str
    as_of: UtcDatetime
    horizon: Timeframe
    horizon_bars: int
    dataset_version: str
    move: Dec
    """End close minus start close, in price units."""
    spread: Dec | None
    available_at: UtcDatetime
    source_refs: list[str]


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    snapshot: FeatureSnapshot
    timeframe: Timeframe
    provenance: Provenance
    mode: Mode | None
    dataset_version: str


def _ts(t: datetime) -> str:
    return t.isoformat()


class BarLog:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    # --- writing -----------------------------------------------------------------------

    def record(
        self,
        snapshot: FeatureSnapshot,
        *,
        timeframe: Timeframe,
        provenance: Provenance,
        mode: Mode | None,
        dataset_version: str,
    ) -> bool:
        """Store the snapshot of one closed bar; False if that bar is already recorded."""
        with transaction(self._conn):
            return bool(self._conn.execute(
                "INSERT INTO learning_snapshots (symbol, timeframe, as_of, feature_schema,"
                " dataset_version, provenance, mode, quality, available_at, body, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (snapshot.symbol, timeframe.value, _ts(snapshot.as_of), snapshot.feature_schema,
                 dataset_version, provenance.value, None if mode is None else mode.value,
                 snapshot.quality.value, _ts(snapshot.available_at),
                 snapshot.model_dump_json(), _ts(self._clock())),
            ).rowcount)  # fmt: skip

    def label_due(
        self, candles: CandleStore, *, symbol: str, horizons: list[Horizon], now: datetime
    ) -> list[BarOutcome]:
        """Label every recorded bar whose horizon has ended by `now`; store and return the
        new outcomes. No wall-clock window: a back-fill run long after its data, or a
        recorder that was down, still labels everything."""
        new: list[BarOutcome] = []
        version = candles.dataset_version
        for h in horizons:
            rows = self._conn.execute(
                "SELECT DISTINCT s.as_of FROM learning_snapshots s WHERE s.symbol = ?"
                " AND s.dataset_version = ? AND s.as_of <= ? AND NOT EXISTS"
                " (SELECT 1 FROM learning_outcomes o WHERE o.symbol = s.symbol"
                " AND o.as_of = s.as_of AND o.horizon = ? AND o.horizon_bars = ?"
                " AND o.dataset_version = s.dataset_version) ORDER BY s.as_of",
                (symbol, version, _ts(now - h.length), h.timeframe.value, h.bars),
            ).fetchall()  # fmt: skip
            for (as_of_text,) in rows:
                outcome = self._outcome(candles, symbol, h, datetime.fromisoformat(as_of_text),
                                        now)  # fmt: skip
                if outcome is not None and self._store(outcome):
                    new.append(outcome)
        return new

    def _outcome(
        self, candles: CandleStore, symbol: str, h: Horizon, as_of: datetime, now: datetime
    ) -> BarOutcome | None:
        step = TIMEFRAME_DURATION[h.timeframe]
        start = candles.bar_at(symbol, h.timeframe, as_of - step, as_of=now)
        end = candles.bar_at(symbol, h.timeframe, as_of + h.length - step, as_of=now)
        if start is None or end is None:
            return None  # a missing start or end bar: no label, never a stretched one
        return BarOutcome(
            symbol=symbol, as_of=as_of, horizon=h.timeframe, horizon_bars=h.bars,
            dataset_version=candles.dataset_version, move=end.close - start.close,
            spread=start.spread, available_at=end.available_at,
            source_refs=[candles.event_id(start), candles.event_id(end)],
        )  # fmt: skip

    def _store(self, o: BarOutcome) -> bool:
        with transaction(self._conn):
            return bool(self._conn.execute(
                "INSERT INTO learning_outcomes (symbol, as_of, horizon, horizon_bars,"
                " dataset_version, move, spread, available_at, body, labeled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (o.symbol, _ts(o.as_of), o.horizon.value, o.horizon_bars, o.dataset_version,
                 str(o.move), None if o.spread is None else str(o.spread),
                 _ts(o.available_at), o.model_dump_json(), _ts(self._clock())),
            ).rowcount)  # fmt: skip

    # --- reading -----------------------------------------------------------------------

    def last_recorded(
        self, symbol: str, timeframe: Timeframe, feature_schema: str, dataset_version: str
    ) -> tuple[datetime, datetime] | None:
        """(as_of, available_at) of the newest recorded bar, if any."""
        row = self._conn.execute(
            "SELECT as_of, available_at FROM learning_snapshots WHERE symbol = ?"
            " AND timeframe = ? AND feature_schema = ? AND dataset_version = ?"
            " ORDER BY as_of DESC LIMIT 1",
            (symbol, timeframe.value, feature_schema, dataset_version),
        ).fetchone()
        return None if row is None else (datetime.fromisoformat(row[0]),
                                         datetime.fromisoformat(row[1]))  # fmt: skip

    def snapshots(
        self, symbol: str, timeframe: Timeframe, *, start: datetime, end: datetime
    ) -> list[SnapshotRow]:
        """Recorded snapshots with `start <= as_of < end`, oldest first."""
        rows = self._conn.execute(
            "SELECT body, provenance, mode, dataset_version FROM learning_snapshots"
            " WHERE symbol = ? AND timeframe = ? AND as_of >= ? AND as_of < ?"
            " ORDER BY as_of, dataset_version",
            (symbol, timeframe.value, _ts(start), _ts(end)),
        ).fetchall()
        return [
            SnapshotRow(
                FeatureSnapshot.model_validate_json(b),
                timeframe,
                Provenance(p),
                None if m is None else Mode(m),
                v,
            )
            for b, p, m, v in rows
        ]

    def outcomes(
        self, symbol: str, horizon: Horizon, *, start: datetime, end: datetime
    ) -> list[BarOutcome]:
        """Outcomes with `start <= as_of < end`, oldest first."""
        rows = self._conn.execute(
            "SELECT body FROM learning_outcomes WHERE symbol = ? AND horizon = ?"
            " AND horizon_bars = ? AND as_of >= ? AND as_of < ? ORDER BY as_of, dataset_version",
            (symbol, horizon.timeframe.value, horizon.bars, _ts(start), _ts(end)),
        ).fetchall()
        return [BarOutcome.model_validate_json(b) for (b,) in rows]
