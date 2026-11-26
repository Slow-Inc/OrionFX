"""Versioned strategy/technique/composition registry and eligibility history (#20;
master plan §15.5, §16.4; UST-01, UST-08; AT-28).

- A (kind, id, version) is immutable: re-registering the same hash is a no-op, a
  different hash for an existing version is refused (code changes make versions).
- Eligibility is append-only history; the newest record is current. A record must
  name a registered subject and its exact hash, so it can never approve code that
  was swapped after review. Every change is audited.
"""

import sqlite3
from typing import Literal

from app.core.contracts import EligibilityRecord, StrategyManifest
from app.core.time import Clock, utc_now
from app.storage.audit import audit
from app.storage.db import transaction

Kind = Literal["strategy", "technique", "composition", "model"]


class RegistryConflict(ValueError):
    """The request contradicts what is already registered."""


class UnknownSubject(KeyError):
    pass


def _ref(manifest: StrategyManifest) -> str:
    return f"{manifest.strategy_id}@{manifest.version}"


class ArtifactRegistry:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def register(self, kind: Kind, manifest: StrategyManifest, *, actor: str) -> None:
        with transaction(self._conn):
            existing = self.get(kind, manifest.strategy_id, manifest.version)
            if existing is not None:
                if existing.artifact_hash != manifest.artifact_hash:
                    raise RegistryConflict(
                        f"{kind} {_ref(manifest)} is registered with another hash;"
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
                detail=f"{kind} {_ref(manifest)} {manifest.artifact_hash}",
            )

    def get(self, kind: Kind, subject_id: str, version: str) -> StrategyManifest | None:
        row = self._conn.execute(
            "SELECT manifest FROM registry_artifacts"
            " WHERE kind = ? AND subject_id = ? AND version = ?",
            (kind, subject_id, version),
        ).fetchone()
        return None if row is None else StrategyManifest.model_validate_json(row[0])

    def versions(self, kind: Kind, subject_id: str) -> list[StrategyManifest]:
        rows = self._conn.execute(
            "SELECT manifest FROM registry_artifacts WHERE kind = ? AND subject_id = ?"
            " ORDER BY registered_at, version",
            (kind, subject_id),
        )
        return [StrategyManifest.model_validate_json(r[0]) for r in rows]

    def set_eligibility(self, record: EligibilityRecord, *, actor: str) -> None:
        kind: Kind = record.subject_kind
        subject_id, _, version = record.subject_ref.partition("@")
        with transaction(self._conn):
            manifest = self.get(kind, subject_id, version)
            if manifest is None:
                raise UnknownSubject(f"{kind} {record.subject_ref} is not registered")
            if manifest.artifact_hash != record.subject_hash:
                raise RegistryConflict(
                    f"eligibility for {record.subject_ref} names another artifact hash"
                )
            self._conn.execute(
                "INSERT INTO registry_eligibility (kind, subject_ref, subject_hash, record,"
                " recorded_at, recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    record.subject_ref,
                    record.subject_hash,
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

    def eligibility(self, kind: Kind, subject_ref: str) -> EligibilityRecord | None:
        row = self._conn.execute(
            "SELECT record FROM registry_eligibility WHERE kind = ? AND subject_ref = ?"
            " ORDER BY id DESC LIMIT 1",
            (kind, subject_ref),
        ).fetchone()
        return None if row is None else EligibilityRecord.model_validate_json(row[0])
