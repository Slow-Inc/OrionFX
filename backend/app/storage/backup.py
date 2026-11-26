"""Verified backup and restore (PRD-08 FR-008, SYS-05, AT-22).

A restore can put older state on disk than the broker's reality, so it always
sets `requires_reconcile`; M06 must reconcile with the broker and clear it
before any new exposure (§9.3).
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.storage.db import connect
from app.storage.flags import REQUIRES_RECONCILE, set_flag


@dataclass(frozen=True, slots=True)
class BackupReport:
    path: Path
    ok: bool
    row_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}


def verify_backup(path: Path) -> BackupReport:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                return BackupReport(path, ok=False, error=str(check))
            return BackupReport(path, ok=True, row_counts=_row_counts(conn))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return BackupReport(path, ok=False, error=str(exc))


def backup_database(conn: sqlite3.Connection, dest: Path) -> BackupReport:
    """Online backup, then verify the copy and compare row counts with the source."""
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    report = verify_backup(dest)
    if report.ok and report.row_counts != _row_counts(conn):
        return BackupReport(dest, ok=False, row_counts=report.row_counts, error="count mismatch")
    return report


def restore_database(src: Path, db_path: Path) -> BackupReport:
    """Copy a verified backup over `db_path` and demand broker reconciliation."""
    report = verify_backup(src)
    if not report.ok:
        raise ValueError(f"refusing to restore unverified backup: {report.error}")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = connect(db_path)
    try:
        source.backup(target)
        set_flag(target, REQUIRES_RECONCILE, "true")
    finally:
        source.close()
        target.close()
    return report
