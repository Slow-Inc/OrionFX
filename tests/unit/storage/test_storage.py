"""M08 slice A: migrations, journal, lease, backup/restore, flags (PRD-08 FR-001..003, 007, 008)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.enums import Mode
from app.core.events import make_event
from app.storage.backup import backup_database, restore_database, verify_backup
from app.storage.db import connect, migrate
from app.storage.flags import REQUIRES_RECONCILE, get_flag, set_flag
from app.storage.journal import Journal
from app.storage.lease import LeaseStore


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "orionfx.db")
    migrate(c)
    yield c
    c.close()


def _health_event() -> object:
    return make_event(
        "health.updated",
        source="M08",
        payload={"component": "M02", "status": "OK", "last_heartbeat": datetime.now(UTC)},
        account_id="acc",
        bot_id="bot",
        mode=Mode.DEMO,
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


# --- migrations --------------------------------------------------------------


def test_migrations_apply_once(tmp_path: Path) -> None:
    c = connect(tmp_path / "db.sqlite")
    first = migrate(c)
    assert first and first[0].startswith("0001")
    assert migrate(c) == []
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "consumer_offsets", "config_versions", "executor_leases"} <= tables


def test_connection_uses_wal(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_ok.sql").write_text("CREATE TABLE a(x INTEGER);")
    (mig / "0002_bad.sql").write_text("CREATE TABLE b(x INTEGER); THIS IS NOT SQL;")
    c = connect(tmp_path / "db.sqlite")
    with pytest.raises(sqlite3.Error):
        migrate(c, mig)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables
    applied = [r[0] for r in c.execute("SELECT version FROM schema_migrations")]
    assert applied == ["0001_ok"]


# --- journal -----------------------------------------------------------------


def test_journal_dedupe(conn: sqlite3.Connection) -> None:
    j = Journal(conn)
    env = _health_event()
    seq = j.append(env)  # type: ignore[arg-type]
    assert seq == 1
    assert j.append(env) is None  # type: ignore[arg-type]
    assert j.latest_seq() == 1


def test_journal_round_trip_and_order(conn: sqlite3.Connection) -> None:
    j = Journal(conn)
    envs = [_health_event() for _ in range(3)]
    for e in envs:
        j.append(e)  # type: ignore[arg-type]
    got = j.read_since(0)
    assert [s.seq for s in got] == [1, 2, 3]
    assert [s.envelope for s in got] == envs
    assert [s.seq for s in j.read_since(2)] == [3]


def test_journal_filters_by_type(conn: sqlite3.Connection) -> None:
    j = Journal(conn)
    j.append(_health_event())  # type: ignore[arg-type]
    assert j.read_since(0, event_types=["trade.intent"]) == []


def test_consumer_offsets(conn: sqlite3.Connection) -> None:
    j = Journal(conn)
    assert j.committed_offset("M06") == 0
    j.commit_offset("M06", 5)
    j.commit_offset("M06", 3)  # never moves backwards
    assert j.committed_offset("M06") == 5


# --- lease -------------------------------------------------------------------


def test_lease_single_holder(conn: sqlite3.Connection) -> None:
    clock = Clock()
    leases = LeaseStore(conn, clock=clock)
    assert leases.acquire("acc:XAUUSD", "host-a", ttl=timedelta(seconds=30))
    assert not leases.acquire("acc:XAUUSD", "host-b", ttl=timedelta(seconds=30))
    assert leases.acquire("acc:XAUUSD", "host-a", ttl=timedelta(seconds=30))  # renew
    assert leases.holder("acc:XAUUSD") == "host-a"


def test_lease_expired_can_be_taken_over(conn: sqlite3.Connection) -> None:
    clock = Clock()
    leases = LeaseStore(conn, clock=clock)
    leases.acquire("ns", "host-a", ttl=timedelta(seconds=30))
    clock.now += timedelta(seconds=31)
    assert leases.holder("ns") is None
    assert leases.acquire("ns", "host-b", ttl=timedelta(seconds=30))


def test_lease_release_only_by_holder(conn: sqlite3.Connection) -> None:
    leases = LeaseStore(conn, clock=Clock())
    leases.acquire("ns", "host-a", ttl=timedelta(seconds=30))
    assert not leases.release("ns", "host-b")
    assert leases.release("ns", "host-a")
    assert leases.holder("ns") is None


# --- backup / restore ----------------------------------------------------------


def test_backup_restore_reproduces_state_and_demands_reconcile(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    c = connect(db)
    migrate(c)
    Journal(c).append(_health_event())  # type: ignore[arg-type]
    set_flag(c, "risk_lock", "true")
    report = backup_database(c, tmp_path / "bk.db")
    assert report.ok and report.row_counts["events"] == 1

    Journal(c).append(_health_event())  # type: ignore[arg-type]
    set_flag(c, "risk_lock", "false")
    c.close()

    restore_database(tmp_path / "bk.db", db)
    r = connect(db)
    assert Journal(r).latest_seq() == 1
    assert get_flag(r, "risk_lock") == "true"
    assert get_flag(r, REQUIRES_RECONCILE) == "true"


def test_verify_backup_rejects_corrupt_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    assert verify_backup(bad).ok is False


def test_backup_path_with_hash_and_space(tmp_path: Path) -> None:
    d = tmp_path / "x y#z"
    d.mkdir()
    c = connect(d / "live.db")
    migrate(c)
    Journal(c).append(_health_event())  # type: ignore[arg-type]
    report = backup_database(c, d / "bk 1.db")
    assert report.ok, report.error
    assert report.row_counts["events"] == 1


def test_nested_transaction_is_atomic(conn: sqlite3.Connection) -> None:
    from app.storage.db import transaction

    with pytest.raises(RuntimeError), transaction(conn):
        set_flag(conn, "a", "1")  # nested -> savepoint
        raise RuntimeError("boom")
    assert get_flag(conn, "a") is None
    assert not conn.in_transaction


def test_concurrent_migrate_second_process_skips(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    first, second = connect(db), connect(db)
    second.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)"
    )
    migrate(first)
    assert migrate(second) == []


def test_split_keeps_trigger_body_whole(tmp_path: Path) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_t.sql").write_text(
        "CREATE TABLE a(x INTEGER);\n"
        "CREATE TABLE b(n INTEGER);\n"
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN INSERT INTO b VALUES (1); END;\n"
    )
    c = connect(tmp_path / "db.sqlite")
    migrate(c, mig)
    c.execute("INSERT INTO a VALUES (1)")
    assert c.execute("SELECT COUNT(*) FROM b").fetchone()[0] == 1
