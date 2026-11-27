"""Experience log and point-in-time outcome labels (#59; LRN-02, LRN-06; AT-15).

Every M05 decision, WAIT included, becomes one append-only experience: what the bot
saw (snapshot ref, a small normalised feature vector, regime) and what it decided.

Labels come later, append-only, each stamped with `available_at`, the moment the
outcome became known:
- REALIZED: a traded experience (ENTER/ADD) takes its cycle's realized final Cycle
  Net, known when the cycle closed.
- MARKET_MOVE: any other experience takes the market's close-to-close move over its
  horizon, from closed bars only. It is known when the horizon's last bar became
  available.
A skipped signal never gets a REALIZED (P&L) label: no fill is ever invented. Missing
market data means no label, not a guessed one.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.contracts import ContractModel, DecisionCreated, FeatureSnapshot
from app.core.enums import DecisionAction, Direction, Mode, Timeframe
from app.core.time import TIMEFRAME_DURATION, Clock, utc_now
from app.core.types import Dec, UtcDatetime
from app.decision.strategies import (
    ADX_H1,
    DIR_H1,
    DIR_M5,
    DIR_M15,
    RANGE_M15,
    RSI_M5,
    STRUCT_M15,
)
from app.market_data.store import CandleStore
from app.storage.db import transaction

D = Decimal
TRADED = (DecisionAction.ENTER, DecisionAction.ADD)
VECTOR_SCALE: dict[str, Decimal] = {
    DIR_H1: D(1), DIR_M15: D(1), DIR_M5: D(1), STRUCT_M15: D(1), RANGE_M15: D(1),
    RSI_M5: D(100), ADX_H1: D(100),
}  # fmt: skip
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
    cycle_id: str | None
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


@dataclass(frozen=True)
class Label:
    decision_id: str
    kind: LabelKind
    value: Decimal
    """REALIZED: Cycle Net in USD. MARKET_MOVE: close-to-close move in price units."""
    direction: Direction
    available_at: datetime
    source_refs: list[str]


def vector_of(snapshot: FeatureSnapshot) -> dict[str, Decimal | None]:
    """The normalised feature vector; a missing feature stays None (never 0)."""
    out: dict[str, Decimal | None] = {}
    for key, scale in VECTOR_SCALE.items():
        v = snapshot.values.get(key)
        out[key] = None if v is None else v / scale
    return out


def regime_of(snapshot: FeatureSnapshot) -> str:
    r = snapshot.values.get(RANGE_M15)
    return "UNKNOWN" if r is None else "RANGE" if r == 1 else "TREND"


def _direction(x: Decimal) -> Direction:
    return Direction.UP if x > 0 else Direction.DOWN if x < 0 else Direction.NEUTRAL


class ExperienceLog:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def record(
        self,
        *,
        decision: DecisionCreated,
        snapshot: FeatureSnapshot,
        mode: Mode,
        symbol: str,
        cycle_id: str | None,
        direction: Direction | None,
        horizon: Timeframe,
        horizon_bars: int,
    ) -> Experience:
        exp = Experience(
            decision_id=decision.decision_id, action=decision.action,
            strategy_id=decision.strategy_id, direction=direction, cycle_id=cycle_id,
            mode=mode, policy_version=decision.policy_version, symbol=symbol,
            snapshot_ref=snapshot.ref, as_of=snapshot.as_of, horizon=horizon,
            horizon_bars=horizon_bars, regime=regime_of(snapshot),
            vector=vector_of(snapshot), reason_codes=decision.reason_codes,
        )  # fmt: skip
        body = exp.model_dump_json()
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT body FROM learning_experiences WHERE decision_id = ?",
                (exp.decision_id,),
            ).fetchone()
            if row is not None:
                if row[0] != body:
                    raise ExperienceConflict(f"{exp.decision_id} recorded with another body")
                return exp
            self._conn.execute(
                "INSERT INTO learning_experiences (decision_id, action, strategy_id, direction,"
                " cycle_id, mode, policy_version, symbol, snapshot_ref, as_of, horizon,"
                " horizon_bars, regime, body, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (exp.decision_id, exp.action, exp.strategy_id, exp.direction, exp.cycle_id,
                 exp.mode, exp.policy_version, exp.symbol, exp.snapshot_ref,
                 exp.as_of.isoformat(), exp.horizon, exp.horizon_bars, exp.regime, body,
                 self._clock().isoformat()),
            )  # fmt: skip
        return exp

    def experiences(self) -> list[Experience]:
        rows = self._conn.execute(
            "SELECT body FROM learning_experiences ORDER BY as_of, rowid"
        ).fetchall()
        return [Experience.model_validate_json(r[0]) for r in rows]


def _market_move(exp: Experience, candles: CandleStore, now: datetime) -> Label | None:
    end = exp.as_of + TIMEFRAME_DURATION[exp.horizon] * exp.horizon_bars
    bars = candles.bars(exp.symbol, exp.horizon, as_of=now)
    entry = [b for b in bars if b.close_time <= exp.as_of]
    done = [b for b in bars if b.close_time >= end]
    if not entry or not done:
        return None
    start_bar, end_bar = entry[-1], done[0]
    move = end_bar.close - start_bar.close
    refs = [f"bar:{b.timeframe}:{b.open_time.isoformat()}" for b in (start_bar, end_bar)]
    return Label(exp.decision_id, LabelKind.MARKET_MOVE, move, _direction(move),
                 end_bar.available_at, refs)  # fmt: skip


def _realized(conn: sqlite3.Connection, exp: Experience, now: datetime) -> Label | None:
    if exp.cycle_id is None:
        return None
    row = conn.execute(
        "SELECT final_net, closed_at FROM cycles WHERE cycle_id = ? AND state = 'CLOSED'",
        (exp.cycle_id,),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    closed_at = datetime.fromisoformat(row[1])
    if closed_at > now:
        return None
    net = D(row[0])
    return Label(exp.decision_id, LabelKind.REALIZED, net, _direction(net), closed_at,
                 [f"cycle:{exp.cycle_id}"])  # fmt: skip


def label_due(conn: sqlite3.Connection, candles: CandleStore, *, now: datetime) -> list[Label]:
    """Label every unlabelled experience whose outcome is known at `now`; store and
    return the new labels."""
    rows = conn.execute(
        "SELECT e.body FROM learning_experiences e WHERE NOT EXISTS"
        " (SELECT 1 FROM learning_labels l WHERE l.decision_id = e.decision_id)"
        " ORDER BY e.as_of, e.rowid"
    ).fetchall()
    new: list[Label] = []
    with transaction(conn):
        for (body,) in rows:
            exp = Experience.model_validate_json(body)
            traded = exp.action in TRADED
            label = _realized(conn, exp, now) if traded else _market_move(exp, candles, now)
            if label is None:
                continue
            conn.execute(
                "INSERT INTO learning_labels (decision_id, kind, value, direction,"
                " available_at, source_refs, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (label.decision_id, label.kind, str(label.value), label.direction,
                 label.available_at.isoformat(), json.dumps(label.source_refs),
                 now.isoformat()),
            )  # fmt: skip
            new.append(label)
    return new
