"""Data sources of candle dataset versions (#128, PRD #120 slice 1; DAT-01, DAT-04).

A **data source** is the feed a bar came from: a vendor's history (`dukascopy`,
`firstrate-gc`) or the live broker feed (`mt5`). It is a dimension separate from
LIVE/BACKFILL provenance, which says only whether the bot saw a bar as it happened.

Mechanism: one candle dataset version holds the bars of exactly one data source. The
`candle_sources` table (migration 0014) maps each version to its source, append-only, and
refuses a second source for the same version. Stored bars and learning snapshot rows are
keyed by dataset version, so each has one source without a new column. The binding is
enforced where it is made and read: `import_history` registers and checks it, and export
refuses a version with no source. `CandleStore.save` itself does not check it, so a
caller that writes another feed's bars into a bound version directly is not stopped. The
live recorder's versions are not registered yet (they wait on the MT5 wiring, #17).
Export writes the source on every row.

Each source has its own session schedule (`SOURCE_SCHEDULES`), stored with the mapping so
a later change of the code's mapping is caught rather than silently applied to old bars.
Every entry is [PROPOSED]: none has been checked against real data yet (`audit` on import
reports the fit).

A source may omit minutes with no trade (`NO_TRADE_FILL`): the importer then fills a short
run of them flat instead of calling it a gap. A source's key names its import rules
(schedule, fill limit, which series is read). Only the schedule is stored with the
binding, so changing another rule for stored data needs a new key, not an edit.

`firstrate-gc` is FirstRate's ratio-adjusted continuous GC 1-minute series: gold futures
trades as a proxy for spot XAUUSD. Its prices are not tradable spot levels.
"""

import sqlite3
from collections.abc import Mapping
from datetime import timedelta

from app.core.time import Clock, utc_iso, utc_now
from app.market_data.session import CME_GLOBEX_GC, XAUUSD_NY_1700, SessionSchedule
from app.storage.db import transaction

__all__ = [
    "NO_TRADE_FILL",
    "SOURCE_SCHEDULES",
    "SourceConflict",
    "SourceRegistry",
    "UnknownSource",
]

SOURCE_SCHEDULES: Mapping[str, SessionSchedule] = {
    "dukascopy": XAUUSD_NY_1700,  # [PROPOSED] unverified against Dukascopy XAU/USD data
    "mt5": XAUUSD_NY_1700,  # [PROPOSED] confirm against the broker's symbol spec (#17)
    "firstrate-gc": CME_GLOBEX_GC,  # [PROPOSED] CME Globex hours; no holiday calendar
}
"""XAUUSD session schedule per data source."""

NO_TRADE_FILL: Mapping[str, timedelta] = {
    "firstrate-gc": timedelta(minutes=15),  # [PROPOSED] check the run lengths on import
}
"""The longest run of open-session minutes with no bar that a source's import fills as
"no trade": a flat bar at the previous close, volume 0. Only a source that omits no-trade
minutes has an entry. A longer run, or any hole in another source, is a real gap."""


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
