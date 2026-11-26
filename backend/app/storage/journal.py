"""Append-only event journal with per-consumer offsets (PRD-08 FR-002, FR-003).

Delivery is at-least-once: a consumer reads from its committed offset, handles
events idempotently, then commits. A duplicate `event_id` is never stored twice.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.events import EventEnvelope, parse_event
from app.storage.db import transaction


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    envelope: EventEnvelope


class Journal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, env: EventEnvelope) -> int | None:
        """Store the event; return its sequence number, or None if `event_id` was seen."""
        with transaction(self._conn):
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events (event_id, event_type, occurred_at, recorded_at,"
                " account_id, bot_id, cycle_id, correlation_id, body)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    env.event_id,
                    env.event_type,
                    env.occurred_at.isoformat(),
                    env.recorded_at.isoformat(),
                    env.account_id,
                    env.bot_id,
                    env.cycle_id,
                    env.correlation_id,
                    env.model_dump_json(),
                ),
            )
        return cur.lastrowid if cur.rowcount == 1 else None

    def read_since(
        self, seq: int, *, limit: int = 500, event_types: Sequence[str] | None = None
    ) -> list[StoredEvent]:
        sql = "SELECT seq, body FROM events WHERE seq > ?"
        args: list[object] = [seq]
        if event_types is not None:
            sql += f" AND event_type IN ({','.join('?' * len(event_types))})"
            args.extend(event_types)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [StoredEvent(row["seq"], parse_event(row["body"])) for row in rows]

    def latest_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def committed_offset(self, consumer: str) -> int:
        row = self._conn.execute(
            "SELECT last_seq FROM consumer_offsets WHERE consumer = ?", (consumer,)
        ).fetchone()
        return int(row[0]) if row else 0

    def commit_offset(self, consumer: str, seq: int) -> None:
        """Offsets only move forward, so a late duplicate commit cannot rewind a consumer."""
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO consumer_offsets (consumer, last_seq) VALUES (?, ?)"
                " ON CONFLICT(consumer) DO UPDATE SET last_seq = MAX(last_seq, excluded.last_seq)",
                (consumer, seq),
            )
