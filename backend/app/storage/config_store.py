"""Versioned owner configuration with optimistic concurrency (PRD-08 FR-005)."""

import sqlite3

from app.core.config import UserConfig
from app.core.time import utc_now_iso
from app.storage.audit import audit
from app.storage.db import transaction


class VersionConflict(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"config version is {actual}, request expected {expected}")
        self.expected = expected
        self.actual = actual


class ConfigStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _latest_version(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM config_versions")
        return int(row.fetchone()[0])

    def current(self) -> tuple[int, UserConfig]:
        """Latest (version, config); version 0 is the built-in not-ready default."""
        version = self._latest_version()
        if version == 0:
            return 0, UserConfig()
        row = self._conn.execute(
            "SELECT body FROM config_versions WHERE version = ?", (version,)
        ).fetchone()
        return version, UserConfig.model_validate_json(row["body"])

    def update(self, cfg: UserConfig, *, expected_version: int, actor: str) -> int:
        with transaction(self._conn):
            actual = self._latest_version()
            if actual != expected_version:
                raise VersionConflict(expected_version, actual)
            version = actual + 1
            self._conn.execute(
                "INSERT INTO config_versions (version, body, created_at, actor)"
                " VALUES (?, ?, ?, ?)",
                (version, cfg.model_dump_json(), utc_now_iso(), actor),
            )
            audit(self._conn, actor=actor, action="config.update", detail=f"version {version}")
        return version
