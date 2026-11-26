"""Single active executor per namespace (PRD-08 FR-007, master plan §5.3, AT-26).

Binds processes that share this SQLite file. Cross-host exclusivity needs shared
storage and is open (PRD-08 M08-OD-1).
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from app.core.time import utc_now
from app.storage.db import transaction


class LeaseStore:
    def __init__(
        self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._conn = conn
        self._clock = clock

    def acquire(self, namespace: str, holder: str, *, ttl: timedelta) -> bool:
        """Take or renew the lease. False if another holder's lease is still live."""
        now = self._clock()
        expires = (now + ttl).isoformat()
        with transaction(self._conn):
            cur = self._conn.execute(
                "INSERT INTO executor_leases (namespace, holder, expires_at) VALUES (?, ?, ?)"
                " ON CONFLICT(namespace) DO UPDATE SET holder = excluded.holder,"
                " expires_at = excluded.expires_at"
                " WHERE executor_leases.holder = excluded.holder"
                " OR executor_leases.expires_at <= ?",
                (namespace, holder, expires, now.isoformat()),
            )
        return cur.rowcount == 1

    def holder(self, namespace: str) -> str | None:
        row = self._conn.execute(
            "SELECT holder FROM executor_leases WHERE namespace = ? AND expires_at > ?",
            (namespace, self._clock().isoformat()),
        ).fetchone()
        return str(row[0]) if row else None

    def release(self, namespace: str, holder: str) -> bool:
        with transaction(self._conn):
            cur = self._conn.execute(
                "DELETE FROM executor_leases WHERE namespace = ? AND holder = ?",
                (namespace, holder),
            )
        return cur.rowcount == 1
