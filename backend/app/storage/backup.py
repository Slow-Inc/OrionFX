"""Verified backup and restore (PRD-08 FR-008, SYS-05, AT-22).

A restore can put older state on disk than the broker's reality, so it always
sets `requires_reconcile`; M06 must reconcile with the broker and clear it
before any new exposure (§9.3).
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.storage.db import connect
from app.storage.flags import REQUIRES_RECONCILE, set_bool_flag


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


def _open_readonly(path: Path) -> sqlite3.Connection:
    # as_uri() percent-encodes spaces, '#' and '?', which a raw f-string would not.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def verify_backup(path: Path) -> BackupReport:
    try:
        conn = _open_readonly(path)
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
    """Online backup (a consistent snapshot), then verify the copy's integrity.

    Row counts are reported from the copy. They are not compared with the live
    source, which keeps changing after the snapshot. A copy that fails
    verification is deleted so a later restore cannot pick it up.
    """
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    report = verify_backup(dest)
    if not report.ok:
        dest.unlink(missing_ok=True)
    return report


def restore_database(src: Path, db_path: Path) -> BackupReport:
    """Copy a verified backup over `db_path` and demand broker reconciliation."""
    report = verify_backup(src)
    if not report.ok:
        raise ValueError(f"refusing to restore unverified backup: {report.error}")
    # Mark the image BEFORE it goes live: a crash mid-restore must never leave
    # restored old state without the reconcile demand (AT-22).
    staged = db_path.with_name(db_path.name + ".restore-staging")
    _remove_db_files(staged)
    source = _open_readonly(src)
    staging = connect(staged)
    try:
        source.backup(staging)
        set_bool_flag(staging, REQUIRES_RECONCILE, True)
        target = connect(db_path)
        try:
            staging.backup(target)
        finally:
            target.close()
    finally:
        source.close()
        staging.close()
        _remove_db_files(staged)
    return report


def _remove_db_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
