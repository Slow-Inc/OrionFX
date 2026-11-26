"""Versioned owner configuration with optimistic concurrency (PRD-08 FR-005)."""

import sqlite3

from app.core.config import UserConfig
from app.core.time import utc_now
from app.storage.db import transaction


class VersionConflict(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"config version is {actual}, request expected {expected}")
        self.expected = expected
        self.actual = actual


class ConfigStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def current(self) -> tuple[int, UserConfig]:
        """Latest (version, config); version 0 is the built-in not-ready default."""
        row = self._conn.execute(
            "SELECT version, body FROM config_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, UserConfig()
        return int(row["version"]), UserConfig.model_validate_json(row["body"])

    def update(self, cfg: UserConfig, *, expected_version: int, actor: str) -> int:
        with transaction(self._conn):
            row = self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM config_versions")
            actual = int(row.fetchone()[0])
            if actual != expected_version:
                raise VersionConflict(expected_version, actual)
            version = actual + 1
            now = utc_now().isoformat()
            self._conn.execute(
                "INSERT INTO config_versions (version, body, created_at, actor)"
                " VALUES (?, ?, ?, ?)",
                (version, cfg.model_dump_json(), now, actor),
            )
            self._conn.execute(
                "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
                (now, actor, "config.update", f"version {version}"),
            )
        return version
