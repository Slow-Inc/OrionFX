"""Audit trail for control, approval and settings changes (SYS-08, AT-21)."""

import sqlite3

from app.core.time import utc_now_iso
from app.storage.db import transaction


def audit(conn: sqlite3.Connection, *, actor: str, action: str, detail: str) -> None:
    """Joins the caller's transaction when there is one."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (utc_now_iso(), actor, action, detail),
        )
