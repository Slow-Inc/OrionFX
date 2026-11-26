"""SQLite connection + versioned SQL migrations (PRD-08 FR-001).

stdlib sqlite3, WAL mode, one transaction per migration file. Autocommit is
off at the driver level (`isolation_level=None`) so every write path opens an
explicit transaction with `transaction()`.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from app.core.time import utc_now_iso

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    # SQLite may already have rolled back (SQLITE_FULL, I/O error); never let the
    # cleanup ROLLBACK replace the original exception.
    if conn.in_transaction:
        with suppress(sqlite3.Error):
            conn.execute("ROLLBACK")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT. A nested call becomes a SAVEPOINT, so store
    operations compose atomically inside a caller's transaction.

    One connection must not be shared by concurrent threads: give each thread
    (or request) its own connection.
    """
    if conn.in_transaction:
        conn.execute("SAVEPOINT nested")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK TO nested")
            conn.execute("RELEASE nested")
            raise
        conn.execute("RELEASE nested")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _rollback_quietly(conn)
        raise


def _statements(script: str) -> list[str]:
    """Split a script into complete statements (safe for `;` inside strings/triggers)."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)  # an incomplete tail still runs, so a typo fails loudly
    return out


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply unapplied `NNNN_name.sql` files in order; return the versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)"
    )
    applied: list[str] = []
    for path in sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = path.stem
        statements = _statements(path.read_text(encoding="utf-8"))
        with transaction(conn):
            # Checked under the write lock: a second process migrating at the same
            # time finds the version applied and skips it.
            seen = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if seen:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, utc_now_iso()),
            )
        applied.append(version)
    return applied
