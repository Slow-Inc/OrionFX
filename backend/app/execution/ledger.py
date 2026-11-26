"""Durable execution ledger (PRD-06 #13, master plan §8.3, AT-04).

A request row is written PENDING before anything reaches the broker. While any
request is PENDING or UNKNOWN, no new broker write may be dispatched.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.enums import ExecutionState, RequestKind
from app.core.time import Clock, utc_now
from app.storage.db import transaction

OPEN_STATES = (ExecutionState.PENDING, ExecutionState.UNKNOWN)
_OPEN_MARKS = ",".join("?" * len(OPEN_STATES))


class DuplicateRequest(Exception):
    pass


@dataclass(frozen=True, slots=True)
class LedgerRow:
    request_id: str
    kind: RequestKind
    state: ExecutionState
    intent_id: str | None
    cycle_id: str | None
    symbol: str | None
    position_id: str | None
    requested_volume: Decimal
    created_at: datetime


class ExecutionLedger:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def has_unresolved(self) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM exec_requests WHERE state IN ({_OPEN_MARKS}) LIMIT 1",
            [str(s) for s in OPEN_STATES],
        ).fetchone()
        return row is not None

    def create(
        self,
        *,
        request_id: str,
        kind: RequestKind,
        requested_volume: Decimal,
        intent_id: str | None = None,
        cycle_id: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        position_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        """Insert PENDING. The primary key is the idempotency check."""
        now = self._clock().isoformat()
        try:
            with transaction(self._conn):
                self._conn.execute(
                    "INSERT INTO exec_requests (request_id, kind, intent_id, cycle_id, symbol,"
                    " side, position_id, order_id, requested_volume, state, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        kind,
                        intent_id,
                        cycle_id,
                        symbol,
                        side,
                        position_id,
                        order_id,
                        str(requested_volume),
                        ExecutionState.PENDING,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRequest(f"request {request_id} was already recorded") from exc

    def update(
        self,
        request_id: str,
        state: ExecutionState,
        *,
        filled_volume: Decimal | None = None,
        fill_price: Decimal | None = None,
        broker_position_id: str | None = None,
        broker_deal_ids: tuple[str, ...] = (),
        error: str | None = None,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE exec_requests SET state = ?, filled_volume = ?, fill_price = ?,"
                " broker_position_id = ?, broker_deal_ids = ?, error = ?, updated_at = ?"
                " WHERE request_id = ?",
                (
                    state,
                    None if filled_volume is None else str(filled_volume),
                    None if fill_price is None else str(fill_price),
                    broker_position_id,
                    json.dumps(list(broker_deal_ids)),
                    error,
                    self._clock().isoformat(),
                    request_id,
                ),
            )

    def state(self, request_id: str) -> ExecutionState | None:
        row = self._conn.execute(
            "SELECT state FROM exec_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else ExecutionState(row[0])

    def order_of(self, request_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT order_id FROM exec_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else row[0]

    def unresolved(self) -> list[LedgerRow]:
        """Requests whose outcome the broker has not confirmed (PENDING after a crash,
        or UNKNOWN after a lost reply), oldest first."""
        rows = self._conn.execute(
            "SELECT request_id, kind, state, intent_id, cycle_id, symbol, position_id,"
            f" requested_volume, created_at FROM exec_requests WHERE state IN ({_OPEN_MARKS})"
            " ORDER BY created_at, request_id",
            [str(s) for s in OPEN_STATES],
        ).fetchall()
        return [
            LedgerRow(
                r["request_id"],
                RequestKind(r["kind"]),
                ExecutionState(r["state"]),
                r["intent_id"],
                r["cycle_id"],
                r["symbol"],
                r["position_id"],
                Decimal(r["requested_volume"]),
                datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
