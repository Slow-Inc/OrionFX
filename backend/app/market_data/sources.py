"""Data sources of candle dataset versions (#128, PRD #120 slice 1; DAT-01, DAT-04).

A **data source** is the feed a bar came from: a vendor's history (`dukascopy`) or the
live broker feed (`mt5`). It is a dimension separate from LIVE/BACKFILL provenance, which
says only whether the bot saw a bar as it happened.

Mechanism: one candle dataset version holds the bars of exactly one data source. The
`candle_sources` table (migration 0014) maps each version to its source, append-only, and
refuses a second source for the same version. Stored bars and learning snapshot rows are
keyed by dataset version, so each has exactly one source without a new column, and two
feeds can never be mixed under one version. Export writes the source on every row.

Each source has its own session schedule (`SOURCE_SCHEDULES`), stored with the mapping so
a later change of the code's mapping is caught rather than silently applied to old bars.
Both entries are [PROPOSED]: neither has been checked against real data yet (`audit` on
import reports the fit).
"""

import sqlite3
from collections.abc import Mapping

from app.core.time import Clock, utc_iso, utc_now
from app.market_data.session import XAUUSD_NY_1700, SessionSchedule
from app.storage.db import transaction

__all__ = ["SOURCE_SCHEDULES", "SourceConflict", "SourceRegistry", "UnknownSource"]

SOURCE_SCHEDULES: Mapping[str, SessionSchedule] = {
    "dukascopy": XAUUSD_NY_1700,  # [PROPOSED] unverified against Dukascopy XAU/USD data
    "mt5": XAUUSD_NY_1700,  # [PROPOSED] confirm against the broker's symbol spec (#17)
}
"""XAUUSD session schedule per data source."""


class UnknownSource(ValueError):
    pass


class SourceConflict(ValueError):
    pass


class SourceRegistry:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def register(self, dataset_version: str, data_source: str) -> SessionSchedule:
        """Bind a dataset version to its data source (a no-op if already bound to it) and
        return that source's schedule."""
        schedule = SOURCE_SCHEDULES.get(data_source)
        if schedule is None:
            raise UnknownSource(f"no session schedule for data source {data_source!r}")
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO candle_sources (dataset_version, data_source, schedule_id,"
                " registered_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (dataset_version, data_source, schedule.schedule_id, utc_iso(self._clock())),
            )
            stored = self._row(dataset_version)
        if stored != (data_source, schedule.schedule_id):
            raise SourceConflict(f"dataset {dataset_version!r} is bound to {stored}")
        return schedule

    def source_of(self, dataset_version: str) -> str | None:
        row = self._row(dataset_version)
        return None if row is None else row[0]

    def schedule_of(self, dataset_version: str) -> SessionSchedule:
        """The schedule a dataset version's bars were validated against; refused if the
        version has no source or the code now maps its source to another schedule."""
        row = self._row(dataset_version)
        if row is None:
            raise UnknownSource(f"dataset {dataset_version!r} has no data source")
        schedule = SOURCE_SCHEDULES.get(row[0])
        if schedule is None or schedule.schedule_id != row[1]:
            raise SourceConflict(f"dataset {dataset_version!r} was validated against {row[1]}")
        return schedule

    def _row(self, dataset_version: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT data_source, schedule_id FROM candle_sources WHERE dataset_version = ?",
            (dataset_version,),
        ).fetchone()
        return None if row is None else (row[0], row[1])
