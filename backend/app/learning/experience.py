"""Experience log and point-in-time outcome labels (#59; LRN-02, LRN-06; AT-15).

Every M05 decision, WAIT included, becomes one append-only experience: what the bot
saw (snapshot ref, a small normalised feature vector, regime) and what it decided.

Labels come later, append-only, each stamped with `available_at`, the moment the
outcome became known:
- REALIZED: an experience whose intent actually filled (an OPEN execution with a
  filled volume for its intent id) takes its cycle's final realized net. That value
  is known when the cycle closed, and a trigger freezes it from then on.
- MARKET_MOVE: every other experience, including a refused or unfilled ENTER, takes
  the raw close-to-close market move over exactly its horizon: the bar closing at
  `as_of` to the bar closing at the horizon end. It is known when that last bar
  became available. It is side-independent; read it with the experience's
  `direction`. For an EXIT, a DOWN move after closing a BUY basket means the exit
  was right.
No fill is ever invented: a P&L label needs a real fill. Missing or misaligned
market data (a gap, a weekend) means no label, never a stretched or guessed one.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.contracts import ContractModel, DecisionCreated, FeatureSnapshot
from app.core.enums import CycleState, DecisionAction, Direction, Mode, Timeframe
from app.core.time import TIMEFRAME_DURATION, Clock, utc_iso, utc_now
from app.core.types import Dec, UtcDatetime
from app.learning.moves import bars_closing_at
from app.market_data.features import feature_key
from app.market_data.store import CandleStore
from app.storage.db import transaction

D = Decimal
H1, M15, M5 = Timeframe.H1, Timeframe.M15, Timeframe.M5
TREND_M15 = feature_key(M15, "CAT-T03-06", "trend")
RANGE_M15 = feature_key(M15, "CAT-T03-06", "range")
VECTOR_SCALE: dict[str, Decimal] = {
    feature_key(H1, "CAT-T03-07", "direction"): D(1),
    feature_key(M15, "CAT-T03-07", "direction"): D(1),
    feature_key(M5, "CAT-T03-07", "direction"): D(1),
    feature_key(M15, "CAT-T03-04", "structure"): D(1),
    RANGE_M15: D(1),
    TREND_M15: D(1),
    feature_key(M5, "CAT-T01-04", "rsi"): D(100),
    feature_key(H1, "CAT-T01-07", "adx"): D(100),
}
"""Feature key -> divisor, so every dimension is roughly in [-1, 1]."""


class ExperienceConflict(ValueError):
    """A decision id was recorded with a different body."""


class LabelKind(StrEnum):
    REALIZED = "REALIZED"
    MARKET_MOVE = "MARKET_MOVE"


class Experience(ContractModel):
    decision_id: str
    action: DecisionAction
    strategy_id: str | None
    direction: Direction | None
    """The side the decision bet on (against the held side for an EXIT), if any."""
    cycle_id: str | None
    intent_id: str | None
    mode: Mode
    policy_version: str
    symbol: str
    snapshot_ref: str
    as_of: UtcDatetime
    horizon: Timeframe
    horizon_bars: int
    regime: str
    vector: dict[str, Dec | None]
    reason_codes: list[str]

    @property
    def horizon_end(self) -> datetime:
        return self.as_of + TIMEFRAME_DURATION[self.horizon] * self.horizon_bars


class Label(ContractModel):
    decision_id: str
    kind: LabelKind
    value: Dec
    """REALIZED: final realized net in USD. MARKET_MOVE: close-to-close move, price units."""
    direction: Direction
    available_at: UtcDatetime
    source_refs: list[str]


def vector_of(snapshot: FeatureSnapshot) -> dict[str, Decimal | None]:
    """The normalised feature vector; a missing feature stays None (never 0)."""
    out: dict[str, Decimal | None] = {}
    for key, scale in VECTOR_SCALE.items():
        v = snapshot.values.get(key)
        out[key] = None if v is None else v / scale
    return out


def regime_of(snapshot: FeatureSnapshot) -> str:
    """The M02 regime technique's own flags: TREND, RANGE, NEUTRAL (neither: ADX between
    the two thresholds) or UNKNOWN (not computed)."""
    trend, rng = snapshot.values.get(TREND_M15), snapshot.values.get(RANGE_M15)
    if trend is None or rng is None:
        return "UNKNOWN"
    return "TREND" if trend == 1 else "RANGE" if rng == 1 else "NEUTRAL"


def _direction(x: Decimal) -> Direction:
    return Direction.UP if x > 0 else Direction.DOWN if x < 0 else Direction.NEUTRAL


class ExperienceLog:
    def __init__(
        self, conn: sqlite3.Connection, candles: CandleStore, *, clock: Clock = utc_now
    ) -> None:
        self._conn = conn
        self._candles = candles
        self._clock = clock

    # --- recording ---------------------------------------------------------------------

    def record(
        self,
        *,
        decision: DecisionCreated,
        snapshot: FeatureSnapshot,
        mode: Mode,
        symbol: str,
        cycle_id: str | None,
        intent_id: str | None,
        direction: Direction | None,
        horizon: Timeframe,
        horizon_bars: int,
    ) -> Experience:
        exp = Experience(
            decision_id=decision.decision_id, action=decision.action,
            strategy_id=decision.strategy_id, direction=direction, cycle_id=cycle_id,
            intent_id=intent_id, mode=mode, policy_version=decision.policy_version,
            symbol=symbol, snapshot_ref=snapshot.ref, as_of=snapshot.as_of, horizon=horizon,
            horizon_bars=horizon_bars, regime=regime_of(snapshot),
            vector=vector_of(snapshot), reason_codes=decision.reason_codes,
        )  # fmt: skip
        body = exp.model_dump_json()
        with transaction(self._conn):
            inserted = self._conn.execute(
                "INSERT INTO learning_experiences (decision_id, action, strategy_id, direction,"
                " cycle_id, mode, policy_version, symbol, snapshot_ref, as_of, horizon,"
                " horizon_bars, horizon_end, intent_id, regime, body, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (decision_id) DO NOTHING",
                (exp.decision_id, exp.action, exp.strategy_id, exp.direction, exp.cycle_id,
                 exp.mode, exp.policy_version, exp.symbol, exp.snapshot_ref,
                 utc_iso(exp.as_of), exp.horizon, exp.horizon_bars,
                 utc_iso(exp.horizon_end), exp.intent_id, exp.regime, body,
                 utc_iso(self._clock())),
            ).rowcount  # fmt: skip
            if not inserted:
                (stored,) = self._conn.execute(
                    "SELECT body FROM learning_experiences WHERE decision_id = ?",
                    (exp.decision_id,),
                ).fetchone()
                if stored != body and Experience.model_validate_json(stored) != exp:
                    raise ExperienceConflict(f"{exp.decision_id} recorded with another body")
        return exp

    def experiences(self) -> list[Experience]:
        rows = self._conn.execute(
            "SELECT body FROM learning_experiences ORDER BY as_of, rowid"
        ).fetchall()
        return [Experience.model_validate_json(r[0]) for r in rows]

    def labels(self, *, as_of: datetime) -> dict[str, Label]:
        """Labels already known at `as_of`, by decision id (point-in-time)."""
        rows = self._conn.execute(
            "SELECT body FROM learning_labels WHERE available_at <= ?", (utc_iso(as_of),)
        ).fetchall()
        return {(lab := Label.model_validate_json(r[0])).decision_id: lab for r in rows}

    def known(
        self, *, as_of: datetime, regime: str | None = None
    ) -> list[tuple[Experience, Label]]:
        """Experiences whose label was known at `as_of` (so they also happened before
        it), with that label, oldest first; optionally one regime only."""
        rows = self._conn.execute(
            "SELECT e.body, l.body FROM learning_experiences e"
            " JOIN learning_labels l ON l.decision_id = e.decision_id"
            " WHERE l.available_at <= ? AND (? IS NULL OR e.regime = ?)"
            " ORDER BY e.as_of, e.rowid",
            (utc_iso(as_of), regime, regime),
        ).fetchall()
        return [(Experience.model_validate_json(e), Label.model_validate_json(lab))
                for e, lab in rows]  # fmt: skip

    # --- labelling ---------------------------------------------------------------------

    def _fill_cycle(self, exp: Experience) -> str | None:
        """The cycle of an actual fill for this experience's intent, if any."""
        if exp.intent_id is None:
            return None
        row = self._conn.execute(
            "SELECT cycle_id FROM exec_requests WHERE intent_id = ? AND kind = 'OPEN'"
            " AND CAST(COALESCE(filled_volume, '0') AS REAL) > 0 LIMIT 1",
            (exp.intent_id,),
        ).fetchone()
        return None if row is None else row[0]

    def _realized(self, exp: Experience, cycle_id: str, now: datetime) -> Label | None:
        row = self._conn.execute(
            "SELECT final_net, closed_at FROM cycles WHERE cycle_id = ? AND state = ?"
            " AND final_net IS NOT NULL AND closed_at <= ?",
            (cycle_id, CycleState.CLOSED, utc_iso(now)),
        ).fetchone()
        if row is None:
            return None
        net = D(row[0])
        return Label(decision_id=exp.decision_id, kind=LabelKind.REALIZED, value=net,
                     direction=_direction(net), available_at=datetime.fromisoformat(row[1]),
                     source_refs=[f"cycle:{cycle_id}", f"intent:{exp.intent_id}"])  # fmt: skip

    def _market_move(self, exp: Experience, now: datetime) -> Label | None:
        if exp.horizon_end > now:
            return None
        pair = bars_closing_at(self._candles, exp.symbol, exp.as_of, exp.horizon_end, now=now)
        if pair is None:
            return None  # a gap or missing data: no label, never a stretched one
        start, end = pair
        move = end.close - start.close
        return Label(decision_id=exp.decision_id, kind=LabelKind.MARKET_MOVE, value=move,
                     direction=_direction(move), available_at=end.available_at,
                     source_refs=[self._candles.event_id(start),
                                  self._candles.event_id(end)])  # fmt: skip

    def label_due(self, *, now: datetime) -> list[Label]:
        """Label every unlabelled experience whose outcome is known at `now`; store and
        return the new labels. Safe to run concurrently: a row labelled by another run
        is skipped."""
        new: list[Label] = []
        with transaction(self._conn):
            rows = self._conn.execute(
                "SELECT e.body FROM learning_experiences e WHERE NOT EXISTS"
                " (SELECT 1 FROM learning_labels l WHERE l.decision_id = e.decision_id)"
                " AND (e.intent_id IS NOT NULL OR e.horizon_end <= ?)"
                " ORDER BY e.as_of, e.rowid",
                (utc_iso(now),),
            ).fetchall()
            for (body,) in rows:
                exp = Experience.model_validate_json(body)
                cycle_id = self._fill_cycle(exp)
                label = (
                    self._realized(exp, cycle_id, now)
                    if cycle_id is not None
                    else self._market_move(exp, now)
                )
                if label is None:
                    continue
                inserted = self._conn.execute(
                    "INSERT INTO learning_labels (decision_id, kind, value, direction,"
                    " available_at, body, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (decision_id, kind) DO NOTHING",
                    (label.decision_id, label.kind, str(label.value), label.direction,
                     utc_iso(label.available_at), label.model_dump_json(),
                     utc_iso(self._clock())),
                ).rowcount  # fmt: skip
                if inserted:
                    new.append(label)
        return new
