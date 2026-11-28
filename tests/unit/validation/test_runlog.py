"""The claim run log (#132; training-loop.md §6): append-only runs under a registered
protocol hash, a budget counter of distinct configs, a single-use holdout, and errata."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.validation.test_protocol import make_protocol

from app.validation.protocol import protocol_hash
from app.validation.runlog import (
    AlreadyCounted,
    BudgetExhausted,
    ConfigIdConflict,
    ErrataState,
    HoldoutConsumed,
    NotCounted,
    RunLog,
    RunRecord,
    RunStatus,
    UnknownProtocol,
)

NOW = datetime(2030, 6, 1, tzinfo=UTC)
SHA = "a" * 40


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    from app.storage.db import connect, migrate

    c = connect(tmp_path / "runlog.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def log(conn: sqlite3.Connection) -> RunLog:
    return RunLog(conn, clock=lambda: NOW)


@pytest.fixture
def ph(log: RunLog) -> str:
    return log.register(make_protocol(budget=3))


def run(ph: str, config: str, status: RunStatus = RunStatus.OK, **kw: object) -> RunRecord:
    fields: dict[str, object] = {
        "protocol_hash": ph,
        "config_id": config,
        "config_hash": f"hash-{config}",
        "code_sha": SHA,
        "dataset_version": "ds-1",
        "seeds": [1, 2, 3],
        "metrics": {"brier_skill": 0.01},
        "status": status,
    }
    return RunRecord.model_validate(fields | kw)


def bugfix(ph: str, config: str) -> RunRecord:
    return run(
        ph, config, RunStatus.BUGFIX_RERUN, bug_note="#999 label off by one", bug_fix_sha="b" * 40
    )


# --- protocol registration -------------------------------------------------------------


def test_register_returns_the_protocol_hash_and_is_idempotent(log: RunLog) -> None:
    p = make_protocol()
    assert log.register(p) == protocol_hash(p)
    assert log.register(p) == protocol_hash(p)
    assert log.protocol(protocol_hash(p)) == p


def test_a_version_cannot_be_reregistered_with_other_content(log: RunLog) -> None:
    log.register(make_protocol())
    with pytest.raises(ValueError, match="immutable"):
        log.register(make_protocol(budget=9))


def test_a_run_without_a_registered_protocol_hash_is_refused(log: RunLog) -> None:
    with pytest.raises(UnknownProtocol):
        log.record(run("f" * 64, "B0"))
    assert log.runs("f" * 64) == []


def test_a_malformed_protocol_hash_is_refused() -> None:
    with pytest.raises(ValidationError):
        run("not-a-hash", "B0")


# --- the config counter and the budget -------------------------------------------------


def test_ok_and_failed_runs_count_as_distinct_configs(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    log.record(run(ph, "A1", RunStatus.FAILED))
    assert log.counted_configs(ph) == 2


def test_the_budget_allows_exactly_n_then_refuses(log: RunLog, ph: str) -> None:
    for c in ("B0", "A1", "A2"):
        log.record(run(ph, c))
    with pytest.raises(BudgetExhausted):
        log.record(run(ph, "A3"))
    assert log.counted_configs(ph) == 3
    assert len(log.runs(ph)) == 3


def test_aa_runs_are_logged_but_not_counted(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    for seeds in ([4], [5], [6]):
        log.record(run(ph, "B0", RunStatus.AA, seeds=seeds))
    assert log.counted_configs(ph) == 1
    assert [r.record.status for r in log.runs(ph)].count(RunStatus.AA) == 3


def test_a_bugfix_rerun_is_logged_but_not_counted(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0", RunStatus.FAILED))
    log.record(bugfix(ph, "B0"))
    assert log.counted_configs(ph) == 1
    assert len(log.runs(ph)) == 2


def test_exempt_reruns_still_log_at_full_budget(log: RunLog, ph: str) -> None:
    for c in ("B0", "A1", "A2"):
        log.record(run(ph, c))
    log.record(run(ph, "A2", RunStatus.AA, seeds=[9]))
    log.record(bugfix(ph, "A1"))
    assert log.counted_configs(ph) == 3


def test_an_exempt_status_cannot_smuggle_in_a_new_config(log: RunLog, ph: str) -> None:
    with pytest.raises(NotCounted):
        log.record(run(ph, "B0", RunStatus.AA))
    with pytest.raises(NotCounted):
        log.record(bugfix(ph, "B0"))


def test_a_plain_rerun_of_a_counted_config_is_refused(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    with pytest.raises(AlreadyCounted):
        log.record(run(ph, "B0", seeds=[7]))


def test_a_bugfix_rerun_needs_a_bug_note_and_a_fix_sha(ph: str) -> None:
    with pytest.raises(ValidationError):
        run(ph, "B0", RunStatus.BUGFIX_RERUN)
    with pytest.raises(ValidationError):
        run(ph, "B0", RunStatus.BUGFIX_RERUN, bug_note="x", bug_fix_sha="not-a-sha")
    with pytest.raises(ValidationError):
        run(ph, "B0", RunStatus.OK, bug_note="x", bug_fix_sha=SHA)


def test_a_config_id_names_one_config_hash(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    with pytest.raises(ConfigIdConflict):
        log.record(run(ph, "B0", config_hash="hash-other"))
    with pytest.raises(ConfigIdConflict):
        log.record(run(ph, "B1", config_hash="hash-B0"))


def test_counters_are_per_protocol(log: RunLog, ph: str) -> None:
    other = log.register(make_protocol(version="test-protocol-2"))
    log.record(run(ph, "B0"))
    log.record(run(other, "B0"))
    assert (log.counted_configs(ph), log.counted_configs(other)) == (1, 1)


def test_the_log_records_its_fields(log: RunLog, ph: str) -> None:
    run_id = log.record(run(ph, "B0"))
    [logged] = log.runs(ph)
    assert logged.run_id == run_id
    assert logged.recorded_at == NOW
    assert logged.record == run(ph, "B0")


# --- the single-use holdout ------------------------------------------------------------


def test_the_holdout_opens_once(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    assert not log.holdout_consumed(ph)
    opening = log.open_holdout(ph, "B0")
    assert (opening.config_id, opening.opened_at) == ("B0", NOW)
    assert log.holdout_consumed(ph)
    with pytest.raises(HoldoutConsumed):
        log.open_holdout(ph, "B0")


def test_the_holdout_opens_only_for_a_counted_clean_config(log: RunLog, ph: str) -> None:
    with pytest.raises(NotCounted):
        log.open_holdout(ph, "B0")
    run_id = log.record(run(ph, "B0"))
    log.add_erratum(run_id, ErrataState.INVALID, "scorer bug")
    with pytest.raises(NotCounted):
        log.open_holdout(ph, "B0")
    assert not log.holdout_consumed(ph)


def test_a_failed_config_without_a_rerun_has_no_result(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0", RunStatus.FAILED))
    with pytest.raises(NotCounted):
        log.open_holdout(ph, "B0")


def test_a_bugfix_rerun_supersedes_a_marked_run_for_the_holdout(log: RunLog, ph: str) -> None:
    first = log.record(run(ph, "B0"))
    log.record(bugfix(ph, "B0"))
    log.add_erratum(first, ErrataState.SUPERSEDED, "label bug fixed in the rerun")
    assert log.open_holdout(ph, "B0").config_id == "B0"


def test_an_erratum_on_the_current_result_blocks_the_holdout(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    rerun = log.record(bugfix(ph, "B0"))
    log.add_erratum(rerun, ErrataState.INVALID, "the fix was wrong too")
    with pytest.raises(NotCounted):
        log.open_holdout(ph, "B0")


def test_the_holdout_needs_a_registered_protocol(log: RunLog) -> None:
    with pytest.raises(UnknownProtocol):
        log.open_holdout("f" * 64, "B0")


def test_no_new_config_after_the_holdout_is_consumed(log: RunLog, ph: str) -> None:
    log.record(run(ph, "B0"))
    log.open_holdout(ph, "B0")
    with pytest.raises(HoldoutConsumed):
        log.record(run(ph, "A1"))


# --- retention and errata --------------------------------------------------------------


def test_errata_mark_a_run_without_removing_it_or_refunding_budget(log: RunLog, ph: str) -> None:
    run_id = log.record(run(ph, "B0"))
    log.add_erratum(run_id, ErrataState.INVALID, "leak found in fold 3")
    [logged] = log.runs(ph)
    assert [(e.state, e.reason, e.marked_at) for e in logged.errata] == [
        (ErrataState.INVALID, "leak found in fold 3", NOW)
    ]
    assert log.counted_configs(ph) == 1


def test_an_erratum_needs_a_known_run_and_a_reason(log: RunLog, ph: str) -> None:
    with pytest.raises(KeyError):
        log.add_erratum(12345, ErrataState.WITHDRAWN, "x")
    run_id = log.record(run(ph, "B0"))
    with pytest.raises(ValueError):
        log.add_erratum(run_id, ErrataState.WITHDRAWN, " ")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM claim_runs",
        "UPDATE claim_runs SET status = 'aa'",
        "DELETE FROM claim_protocols",
        "DELETE FROM claim_holdout_openings",
        "DELETE FROM claim_run_errata",
    ],
)
def test_nothing_is_ever_deleted_or_rewritten(
    log: RunLog, ph: str, conn: sqlite3.Connection, sql: str
) -> None:
    run_id = log.record(run(ph, "B0"))
    log.open_holdout(ph, "B0")
    log.add_erratum(run_id, ErrataState.UNCONFIRMED, "below the A/A floor")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_registering_and_opening_the_holdout_are_audited(
    log: RunLog, ph: str, conn: sqlite3.Connection
) -> None:
    log.record(run(ph, "B0"))
    log.open_holdout(ph, "B0")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["validation.protocol", "validation.holdout_opened"]
