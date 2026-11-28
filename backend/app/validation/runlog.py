"""The claim run log: append-only runs under a protocol hash, the config counter, the
single-use holdout, and errata (#132; PRD #120; training-loop.md §6 rules 1, 4, 5, 8, 10).

- **A run counts only under a registered protocol.** `register` stores a protocol by its
  content hash; `record` refuses a run whose hash is not registered.
- **The counter** is the number of distinct `config_hash`es with a counted run (`ok` or
  `failed`: a failed config was still tried). This is N for best-of-N and the deflated
  Sharpe.
  - A new counted config past the protocol's budget is refused (`BudgetExhausted`), and
    so is any new counted config once the holdout is consumed (`HoldoutConsumed`).
  - A second counted run of the same config hash is refused (`AlreadyCounted`): a rerun
    must say what it is.
  - `aa` (seed-only rerun, for the noise floor) and `bugfix-rerun` (with a bug note and
    the fix's commit SHA) are logged but never counted, and only for a config hash that
    is already counted, so relabelling cannot sneak a new config past the budget.
  - An `aa` run must use a seed set no earlier run of its config used
    (`SeedsNotChanged`): a repeated draw has no seed variance and would shrink the A/A
    floor. A `bugfix-rerun` may keep the seeds.
  - A `config_id` names one `config_hash` within a protocol, and the other way round.
- **The holdout opens once** per protocol (`open_holdout`), for a counted config whose
  current result (its latest `ok` or `bugfix-rerun` run) carries no erratum. After that it
  is consumed: development data, and no new config counts.
- **Windows stay honest across versions** (training-loop.md §6 rules 1-2). For every
  other registered version, a window it has *read* is its debug window plus its holdout
  once opened; a window it *seals* is its unread slice plus its holdout while unopened.
  `register` refuses a new version when:
  - its holdout or unread slice overlaps a window another version read (an opened
    holdout is development data now, so a claim needs fresh forward data);
  - its debug window overlaps a window another version seals;
  - its unread slice overlaps another version's holdout, opened or not (that version
    may open it at any time), or its holdout another version's unread slice.

  Two versions may still name the same unopened holdout (an abandoned version hands it
  on), and `open_holdout` refuses a holdout another version already opened. An opened
  holdout may serve a later version as debug data.
- **Stored protocols are verified by their stored bytes**: the sha256 of the body as
  written must equal its hash, so a change in how pydantic serializes cannot make a
  registered version unreadable.
- **Retention:** nothing is updated or deleted (database triggers). Errata mark a run
  with a state, a reason and a time. They never refund budget: N counts what was tried.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, StrictStr, model_validator

from app.core.contracts import ContractModel
from app.core.time import Clock, utc_now
from app.storage.audit import audit
from app.storage.db import transaction
from app.validation.protocol import (
    Protocol,
    ProtocolHashMismatch,
    ProtocolWindow,
    canonical_json,
    protocol_hash,
)

SHA = r"^[0-9a-f]{7,40}$"


class RunStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    BUGFIX_RERUN = "bugfix-rerun"
    AA = "aa"


COUNTED = (RunStatus.OK, RunStatus.FAILED)


class ErrataState(StrEnum):
    INVALID = "INVALID"
    WITHDRAWN = "WITHDRAWN"
    CORRECTED = "CORRECTED"
    UNCONFIRMED = "UNCONFIRMED"
    SUPERSEDED = "SUPERSEDED"


class ProtocolRefusal(Exception):
    """A run log rule refused the operation."""


class UnknownProtocol(ProtocolRefusal):
    pass


class BudgetExhausted(ProtocolRefusal):
    pass


class AlreadyCounted(ProtocolRefusal):
    pass


class NotCounted(ProtocolRefusal):
    pass


class ConfigIdConflict(ProtocolRefusal):
    pass


class HoldoutConsumed(ProtocolRefusal):
    """The holdout was opened: it cannot be opened again, and no new config counts."""


class SeedsNotChanged(ProtocolRefusal):
    """An A/A run repeats a seed set its config already ran with."""


class RunRecord(ContractModel):
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_id: StrictStr = Field(min_length=1)
    config_hash: StrictStr = Field(min_length=1)
    """Hash of the config without its seeds: an A/A rerun keeps it and changes seeds."""
    code_sha: str = Field(pattern=SHA)
    dataset_version: StrictStr = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)
    metrics: dict[str, float]
    """Research scalars for the claim statistics, float by design like `claimstats`.
    Nothing on a money path reads them; amounts of record stay Decimal elsewhere."""
    status: RunStatus
    bug_note: StrictStr | None = Field(default=None, min_length=1)
    """Links the bug a `bugfix-rerun` fixes, e.g. an issue reference."""
    bug_fix_sha: str | None = Field(default=None, pattern=SHA)

    @model_validator(mode="after")
    def _bugfix_is_linked(self) -> Self:
        linked = bool(self.bug_note and self.bug_note.strip()) and self.bug_fix_sha is not None
        if self.status is RunStatus.BUGFIX_RERUN and not linked:
            raise ValueError("a bug-fix rerun needs a bug note and the fix's commit SHA")
        if self.status is not RunStatus.BUGFIX_RERUN and (self.bug_note or self.bug_fix_sha):
            raise ValueError("only a bug-fix rerun carries a bug note and fix SHA")
        return self


@dataclass(frozen=True)
class Erratum:
    state: ErrataState
    reason: str
    marked_at: datetime


@dataclass(frozen=True)
class LoggedRun:
    run_id: int
    record: RunRecord
    recorded_at: datetime
    errata: list[Erratum]


@dataclass(frozen=True)
class HoldoutOpening:
    protocol_hash: str
    config_id: str
    opened_at: datetime


def _window_clash(new: Protocol, other: Protocol, *, opened: bool) -> str | None:
    """Why `new` may not sit beside `other`, or None (see the module docstring)."""
    unread = [other.unread_slice] if other.unread_slice else []
    read = [other.debug_window, *([other.holdout] if opened else [])]
    sealed = [*unread, *([] if opened else [other.holdout])]
    rules: list[tuple[str, ProtocolWindow | None, list[ProtocolWindow]]] = [
        ("the holdout overlaps a window already read", new.holdout, read),
        ("the unread slice overlaps a window already read", new.unread_slice, read),
        ("the debug window reads a sealed window", new.debug_window, sealed),
        ("the unread slice overlaps the holdout", new.unread_slice, [other.holdout]),
        ("the holdout overlaps the unread slice", new.holdout, unread),
    ]
    for reason, mine, theirs in rules:
        if mine and any(mine.overlaps(w) for w in theirs):
            return reason
    return None


def _stored(digest: str, body: str) -> Protocol:
    """A stored protocol: its bytes as written must hash to `digest`. A row that fails
    refuses loudly rather than being skipped (refusal when uncertain)."""
    actual = hashlib.sha256(body.encode()).hexdigest()
    if actual != digest:
        raise ProtocolHashMismatch(f"stored protocol hashes to {actual}, expected {digest}")
    return Protocol.model_validate_json(body)


class RunLog:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    # --- protocols -------------------------------------------------------------------------

    def register(self, protocol: Protocol, *, actor: str = "x02") -> str:
        """Store a protocol by its hash and return the hash. A version is immutable."""
        digest = protocol_hash(protocol)
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT protocol_hash FROM claim_protocols WHERE version = ?", (protocol.version,)
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ValueError(f"protocol {protocol.version} is immutable: use a new version")
                return digest
            for other, opened in self._versions():
                clash = _window_clash(protocol, other, opened=opened)
                if clash:
                    raise HoldoutConsumed(f"{protocol.version}: {clash} of {other.version}")
            self._conn.execute(
                "INSERT INTO claim_protocols (protocol_hash, version, body, registered_at)"
                " VALUES (?, ?, ?, ?)",
                (digest, protocol.version, canonical_json(protocol).decode(), self._now()),
            )
            audit(
                self._conn,
                actor=actor,
                action="validation.protocol",
                detail=f"{protocol.version} {digest}",
            )
        return digest

    def protocol(self, digest: str) -> Protocol | None:
        """The registered protocol, verified by the sha256 of its stored bytes."""
        row = self._conn.execute(
            "SELECT body FROM claim_protocols WHERE protocol_hash = ?", (digest,)
        ).fetchone()
        return None if row is None else _stored(digest, row[0])

    def _versions(self) -> list[tuple[Protocol, bool]]:
        """Every registered protocol, verified, with whether its holdout was opened."""
        rows = self._conn.execute(
            "SELECT p.protocol_hash, p.body, o.protocol_hash IS NOT NULL"
            " FROM claim_protocols p LEFT JOIN claim_holdout_openings o USING (protocol_hash)"
        ).fetchall()
        return [(_stored(digest, body), bool(opened)) for digest, body, opened in rows]

    def _require(self, digest: str) -> Protocol:
        protocol = self.protocol(digest)
        if protocol is None:
            raise UnknownProtocol(f"no registered protocol {digest}")
        return protocol

    # --- runs ------------------------------------------------------------------------------

    def record(self, run: RunRecord) -> int:
        """Append a run if the protocol's rules allow it; return its run id."""
        with transaction(self._conn):
            protocol = self._require(run.protocol_hash)
            self._check_identity(run)
            counted = self._counted_hashes(run.protocol_hash)
            if run.status in COUNTED:
                if run.config_hash in counted:
                    raise AlreadyCounted(
                        f"{run.config_id} is already counted: log a rerun as aa or bugfix-rerun"
                    )
                if self.holdout_consumed(run.protocol_hash):
                    raise HoldoutConsumed("the holdout is consumed: start a new protocol version")
                if len(counted) >= protocol.budget:
                    raise BudgetExhausted(f"budget {protocol.budget} spent: stop and report")
            elif run.config_hash not in counted:
                raise NotCounted(f"a {run.status} run needs a counted config {run.config_id}")
            elif run.status is RunStatus.AA and set(run.seeds) in self._seed_sets(run):
                raise SeedsNotChanged(f"an A/A run of {run.config_id} must change the seeds")
            cur = self._conn.execute(
                "INSERT INTO claim_runs (protocol_hash, config_id, config_hash, status, body,"
                " recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run.protocol_hash,
                    run.config_id,
                    run.config_hash,
                    run.status,
                    run.model_dump_json(),
                    self._now(),
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def _check_identity(self, run: RunRecord) -> None:
        clash = self._conn.execute(
            "SELECT config_id, config_hash FROM claim_runs WHERE protocol_hash = ?"
            " AND (config_id = ?) != (config_hash = ?) LIMIT 1",
            (run.protocol_hash, run.config_id, run.config_hash),
        ).fetchone()
        if clash is not None:
            raise ConfigIdConflict(
                f"{run.config_id}/{run.config_hash} clashes with {clash[0]}/{clash[1]}"
            )

    def _seed_sets(self, run: RunRecord) -> list[set[int]]:
        rows = self._conn.execute(
            "SELECT body FROM claim_runs WHERE protocol_hash = ? AND config_hash = ?",
            (run.protocol_hash, run.config_hash),
        ).fetchall()
        return [set(RunRecord.model_validate_json(r[0]).seeds) for r in rows]

    def _counted_hashes(self, digest: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT config_hash FROM claim_runs WHERE protocol_hash = ?"
            " AND status IN (?, ?)",
            (digest, *COUNTED),
        ).fetchall()
        return {r[0] for r in rows}

    def counted_configs(self, digest: str) -> int:
        """N: distinct configs tried under this protocol (A/A and bug-fix reruns excluded)."""
        return len(self._counted_hashes(digest))

    def runs(self, digest: str) -> list[LoggedRun]:
        errata: dict[int, list[Erratum]] = {}
        for e in self._conn.execute(
            "SELECT e.run_id, e.state, e.reason, e.marked_at FROM claim_run_errata e"
            " JOIN claim_runs r USING (run_id) WHERE r.protocol_hash = ? ORDER BY e.id",
            (digest,),
        ):
            errata.setdefault(e[0], []).append(
                Erratum(ErrataState(e[1]), e[2], datetime.fromisoformat(e[3]))
            )
        rows = self._conn.execute(
            "SELECT run_id, body, recorded_at FROM claim_runs WHERE protocol_hash = ?"
            " ORDER BY run_id",
            (digest,),
        ).fetchall()
        return [
            LoggedRun(
                r[0],
                RunRecord.model_validate_json(r[1]),
                datetime.fromisoformat(r[2]),
                errata.get(r[0], []),
            )
            for r in rows
        ]

    # --- the holdout -----------------------------------------------------------------------

    def open_holdout(self, digest: str, config_id: str, *, actor: str = "x02") -> HoldoutOpening:
        """Record the single opening of this protocol's holdout, for one counted config whose
        current result (its latest `ok` or `bugfix-rerun` run) carries no erratum. An
        erratum on a superseded run does not block: that is what a bug-fix rerun is for."""
        with transaction(self._conn):
            holdout = self._require(digest).holdout
            if self.holdout_consumed(digest):
                raise HoldoutConsumed(f"the holdout of {digest} was already opened")
            for other, opened in self._versions():
                if opened and holdout.overlaps(other.holdout):
                    raise HoldoutConsumed(f"this holdout overlaps the one {other.version} opened")
            latest = self._conn.execute(
                "SELECT run_id, EXISTS (SELECT 1 FROM claim_run_errata e"
                "   WHERE e.run_id = claim_runs.run_id) FROM claim_runs"
                " WHERE protocol_hash = ? AND config_id = ? AND status IN (?, ?)"
                " ORDER BY run_id DESC LIMIT 1",
                (digest, config_id, RunStatus.OK, RunStatus.BUGFIX_RERUN),
            ).fetchone()
            if latest is None:
                raise NotCounted(f"{config_id} has no result under this protocol")
            if latest[1]:
                raise NotCounted(f"the current result of {config_id} carries an erratum")
            opened_at = self._now()
            self._conn.execute(
                "INSERT INTO claim_holdout_openings (protocol_hash, config_id, opened_at)"
                " VALUES (?, ?, ?)",
                (digest, config_id, opened_at),
            )
            audit(
                self._conn,
                actor=actor,
                action="validation.holdout_opened",
                detail=f"{digest} for {config_id}",
            )
        return HoldoutOpening(digest, config_id, datetime.fromisoformat(opened_at))

    def holdout_consumed(self, digest: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM claim_holdout_openings WHERE protocol_hash = ?", (digest,)
            ).fetchone()
            is not None
        )

    # --- errata ----------------------------------------------------------------------------

    def add_erratum(self, run_id: int, state: ErrataState, reason: str) -> None:
        """Mark a run; it stays in the log and keeps its place in the counter."""
        if not reason.strip():
            raise ValueError("an erratum needs a reason")
        with transaction(self._conn):
            if (
                self._conn.execute(
                    "SELECT 1 FROM claim_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(run_id)
            self._conn.execute(
                "INSERT INTO claim_run_errata (run_id, state, reason, marked_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, state, reason, self._now()),
            )

    def _now(self) -> str:
        return self._clock().isoformat()
