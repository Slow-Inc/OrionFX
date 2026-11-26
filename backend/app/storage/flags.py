"""Small persisted system flags (e.g. `requires_reconcile` after a restore)."""

import sqlite3

from app.core.time import utc_now_iso
from app.storage.db import transaction

REQUIRES_RECONCILE = "requires_reconcile"


def get_flag(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT value FROM system_flags WHERE name = ?", (name,)).fetchone()
    return str(row[0]) if row else None


def set_flag(conn: sqlite3.Connection, name: str, value: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO system_flags (name, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (name, value, utc_now_iso()),
        )


def get_bool_flag(conn: sqlite3.Connection, name: str) -> bool:
    return get_flag(conn, name) == "true"


def set_bool_flag(conn: sqlite3.Connection, name: str, value: bool) -> None:
    set_flag(conn, name, "true" if value else "false")
