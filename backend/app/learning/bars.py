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
  move became known, and carries the start bar's spread when known. The move is read on
  the finest timeframe with bars closing at both ends (#125), so `spread` is that bar's
  max spread: since #125 usually the M5 bar's, before it the horizon bar's. A missing
  start or end bar gives no label, never a stretched one: once every stored timeframe has
  moved past the end, the row is marked unlabellable (decided at `now`) and not asked
  again. No neutral threshold is applied: that is a training choice.
- A prediction row is what one loaded M04 model said about one recorded bar (#109),
  made at that bar's close. It is learning data only, never `prediction.generated`, so
  it never reaches trading. Its outcome is the bar's label for the model's horizon.
All three tables are append-only (migrations 0011, 0012). The first writer of a bar wins: if a
LIVE and a BACKFILL recorder both reach it, its provenance is whichever wrote first.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.contracts import ContractModel, FeatureSnapshot, PredictionGenerated
from app.core.enums import Mode, Timeframe
from app.core.time import TIMEFRAME_DURATION, Clock, utc_now
from app.core.types import Dec, UtcDatetime
from app.learning.moves import bars_closing_at, data_frontier
from app.market_data.store import CandleStore
from app.storage.db import transaction

__all__ = ["BarLog", "BarOutcome", "Horizon", "Provenance", "RecordedPrediction", "SnapshotRow"]


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


@dataclass(frozen=True, slots=True)
class RecordedPrediction:
    as_of: datetime
    provenance: Provenance
    """The bar's: a BACKFILL prediction may be in-sample for a model trained on it."""
    prediction: PredictionGenerated


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

    def record_prediction(
        self,
        p: PredictionGenerated,
        snapshot: FeatureSnapshot,
        *,
        provenance: Provenance,
        dataset_version: str,
    ) -> bool:
        """Store what a model said about this bar; False if already recorded."""
        with transaction(self._conn):
            return bool(self._conn.execute(
                "INSERT INTO learning_predictions (symbol, as_of, feature_schema,"
                " dataset_version, model_id, model_version, horizon, horizon_bars, direction,"
                " provenance, body, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (snapshot.symbol, _ts(snapshot.as_of), snapshot.feature_schema, dataset_version,
                 p.model_id, p.model_version, p.horizon.value, p.horizon_bars,
                 p.direction.value, provenance.value, p.model_dump_json(),
                 _ts(self._clock())),
            ).rowcount)  # fmt: skip

    def label_due(
        self, candles: CandleStore, *, symbol: str, horizons: list[Horizon], now: datetime
    ) -> list[BarOutcome]:
        """Label every recorded bar whose horizon has ended by `now`; store and return the
        new outcomes. No wall-clock window: a back-fill run long after its data, or a
        recorder that was down, still labels everything."""
        new: list[BarOutcome] = []
        version = candles.dataset_version
        frontier = data_frontier(candles, symbol, now=now)
        for h in horizons:
            rows = self._conn.execute(
                "SELECT DISTINCT s.as_of FROM learning_snapshots s WHERE s.symbol = ?"
                " AND s.dataset_version = ? AND s.as_of <= ? AND NOT EXISTS"
                " (SELECT 1 FROM learning_outcomes o WHERE o.symbol = s.symbol"
                " AND o.as_of = s.as_of AND o.horizon = ? AND o.horizon_bars = ?"
                " AND o.dataset_version = s.dataset_version) AND NOT EXISTS"
                " (SELECT 1 FROM learning_unlabellable u WHERE u.symbol = s.symbol"
                " AND u.as_of = s.as_of AND u.horizon = ? AND u.horizon_bars = ?"
                " AND u.dataset_version = s.dataset_version AND u.decided_at <= ?)"
                " ORDER BY s.as_of",
                (symbol, version, _ts(now - h.length), h.timeframe.value, h.bars,
                 h.timeframe.value, h.bars, _ts(now)),
            ).fetchall()  # fmt: skip
            for (as_of_text,) in rows:
                as_of = datetime.fromisoformat(as_of_text)
                outcome = self._outcome(candles, symbol, h, as_of, now)
                if outcome is not None:
                    if self._store(outcome):
                        new.append(outcome)
                elif frontier is not None and frontier >= as_of + h.length:
                    self._mark_unlabellable(symbol, h, as_of, version, "MISSING_BAR", now)
        return new

    def _mark_unlabellable(
        self,
        symbol: str,
        h: Horizon,
        as_of: datetime,
        version: str,
        reason: str,
        now: datetime,
    ) -> None:
        """The data has moved past a missing start or end bar: this row will never be
        labelled, so stop asking."""
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO learning_unlabellable (symbol, as_of, horizon, horizon_bars,"
                " dataset_version, reason, decided_at, marked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (symbol, _ts(as_of), h.timeframe.value, h.bars, version, reason, _ts(now),
                 _ts(self._clock())),
            )  # fmt: skip

    def unlabellable(self, symbol: str, h: Horizon, *, dataset_version: str) -> list[datetime]:
        """Recorded bars of one dataset version marked never labellable for this horizon,
        oldest first."""
        rows = self._conn.execute(
            "SELECT as_of FROM learning_unlabellable WHERE symbol = ? AND horizon = ?"
            " AND horizon_bars = ? AND dataset_version = ? ORDER BY as_of",
            (symbol, h.timeframe.value, h.bars, dataset_version),
        ).fetchall()
        return [datetime.fromisoformat(r[0]) for r in rows]

    def _outcome(
        self, candles: CandleStore, symbol: str, h: Horizon, as_of: datetime, now: datetime
    ) -> BarOutcome | None:
        pair = bars_closing_at(candles, symbol, as_of, as_of + h.length, now=now)
        if pair is None:
            return None  # a missing start or end bar: no label, never a stretched one
        start, end = pair
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
            " ORDER BY as_of, dataset_version, feature_schema",
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

    def predictions(
        self,
        symbol: str,
        model_id: str,
        *,
        start: datetime,
        end: datetime,
        model_version: str | None = None,
    ) -> list[RecordedPrediction]:
        """Recorded predictions of `model_id` (one version if given) with
        `start <= as_of < end`, oldest first."""
        rows = self._conn.execute(
            "SELECT as_of, provenance, body FROM learning_predictions WHERE symbol = ?"
            " AND model_id = ? AND (? IS NULL OR model_version = ?) AND as_of >= ?"
            " AND as_of < ? ORDER BY as_of, model_version, dataset_version",
            (symbol, model_id, model_version, model_version, _ts(start), _ts(end)),
        ).fetchall()
        return [_recorded(a, pv, b) for a, pv, b in rows]

    def prediction_outcomes(
        self,
        symbol: str,
        model_id: str,
        *,
        start: datetime,
        end: datetime,
        model_version: str | None = None,
    ) -> list[tuple[RecordedPrediction, BarOutcome | None]]:
        """Each recorded prediction with its bar's outcome over the model's own horizon
        (None until that outcome is known, or if the bar has no label)."""
        rows = self._conn.execute(
            "SELECT p.as_of, p.provenance, p.body, o.body FROM learning_predictions p"
            " LEFT JOIN learning_outcomes o ON o.symbol = p.symbol AND o.as_of = p.as_of"
            " AND o.horizon = p.horizon AND o.horizon_bars = p.horizon_bars"
            " AND o.dataset_version = p.dataset_version"
            " WHERE p.symbol = ? AND p.model_id = ? AND (? IS NULL OR p.model_version = ?)"
            " AND p.as_of >= ? AND p.as_of < ?"
            " ORDER BY p.as_of, p.model_version, p.dataset_version",
            (symbol, model_id, model_version, model_version, _ts(start), _ts(end)),
        ).fetchall()
        return [
            (_recorded(a, pv, pb), None if ob is None else BarOutcome.model_validate_json(ob))
            for a, pv, pb, ob in rows
        ]


def _recorded(as_of: str, provenance: str, body: str) -> RecordedPrediction:
    return RecordedPrediction(
        datetime.fromisoformat(as_of),
        Provenance(provenance),
        PredictionGenerated.model_validate_json(body),
    )
