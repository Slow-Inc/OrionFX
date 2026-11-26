"""Versioned strategy/technique/composition registry and eligibility history (#20;
master plan §15.5, §16.4; UST-01, UST-08; AT-28).

- A (kind, id, version) is immutable: re-registering the identical manifest is a
  no-op; any difference (hash or metadata) is refused, because changes make new
  versions. Storage triggers forbid UPDATE/DELETE too.
- Eligibility is append-only history (FK to the registered version). A record must
  carry the registered artifact hash and may approve only modes the manifest
  supports. The newest unexpired record is current. Every change is audited.
"""

import sqlite3

from app.core.contracts import EligibilityRecord, StrategyManifest, SubjectKind
from app.core.time import Clock, utc_now
from app.storage.audit import audit
from app.storage.db import transaction


class RegistryConflict(ValueError):
    """The request contradicts what is already registered."""


class UnknownSubject(KeyError):
    pass


def subject_ref(subject_id: str, version: str) -> str:
    return f"{subject_id}@{version}"


def split_ref(ref: str) -> tuple[str, str]:
    """Ids and versions cannot contain '@' (contract pattern), so the split is exact."""
    subject_id, sep, version = ref.partition("@")
    if not sep or "@" in version:
        raise UnknownSubject(f"malformed subject ref {ref!r}")
    return subject_id, version


class ArtifactRegistry:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def register(self, kind: SubjectKind, manifest: StrategyManifest, *, actor: str) -> None:
        ref = subject_ref(manifest.strategy_id, manifest.version)
        with transaction(self._conn):
            existing = self.get(kind, manifest.strategy_id, manifest.version)
            if existing is not None:
                if existing != manifest:
                    raise RegistryConflict(
                        f"{kind} {ref} is registered with different content;"
                        " publish a new version instead"
                    )
                return
            self._conn.execute(
                "INSERT INTO registry_artifacts (kind, subject_id, version, artifact_hash,"
                " manifest, registered_at, registered_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    manifest.strategy_id,
                    manifest.version,
                    manifest.artifact_hash,
                    manifest.model_dump_json(),
                    self._clock().isoformat(),
                    actor,
                ),
            )
            audit(
                self._conn,
                actor=actor,
                action="registry.register",
                detail=f"{kind} {ref} {manifest.artifact_hash}",
            )

    def get(self, kind: SubjectKind, subject_id: str, version: str) -> StrategyManifest | None:
        row = self._conn.execute(
            "SELECT manifest FROM registry_artifacts"
            " WHERE kind = ? AND subject_id = ? AND version = ?",
            (kind, subject_id, version),
        ).fetchone()
        return None if row is None else StrategyManifest.model_validate_json(row[0])

    def versions(self, kind: SubjectKind, subject_id: str) -> list[StrategyManifest]:
        """In registration order."""
        rows = self._conn.execute(
            "SELECT manifest FROM registry_artifacts WHERE kind = ? AND subject_id = ?"
            " ORDER BY rowid",
            (kind, subject_id),
        )
        return [StrategyManifest.model_validate_json(r[0]) for r in rows]

    def set_eligibility(self, record: EligibilityRecord, *, actor: str) -> None:
        kind = record.subject_kind
        subject_id, version = split_ref(record.subject_ref)
        with transaction(self._conn):
            manifest = self.get(kind, subject_id, version)
            if manifest is None:
                raise UnknownSubject(f"{kind} {record.subject_ref} is not registered")
            if manifest.artifact_hash != record.subject_hash:
                raise RegistryConflict(
                    f"eligibility for {record.subject_ref} names another artifact hash"
                )
            unsupported = set(record.approved_modes) - set(manifest.supported_modes)
            if unsupported:
                raise RegistryConflict(
                    f"{record.subject_ref} does not support {sorted(unsupported)}"
                )
            self._conn.execute(
                "INSERT INTO registry_eligibility (kind, subject_id, version, record,"
                " recorded_at, recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    subject_id,
                    version,
                    record.model_dump_json(),
                    self._clock().isoformat(),
                    actor,
                ),
            )
            audit(
                self._conn,
                actor=actor,
                action="registry.eligibility",
                detail=f"{kind} {record.subject_ref} -> {record.eligibility}",
            )

    def eligibility(self, kind: SubjectKind, ref: str) -> EligibilityRecord | None:
        """The current record: the newest one, unless it has expired (then None)."""
        subject_id, version = split_ref(ref)
        row = self._conn.execute(
            "SELECT record FROM registry_eligibility"
            " WHERE kind = ? AND subject_id = ? AND version = ? ORDER BY id DESC LIMIT 1",
            (kind, subject_id, version),
        ).fetchone()
        if row is None:
            return None
        record = EligibilityRecord.model_validate_json(row[0])
        if record.expires_at is not None and record.expires_at <= self._clock():
            return None
        return record
