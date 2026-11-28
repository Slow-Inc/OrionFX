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

`dukascopy-xauusd` is Dukascopy's public XAUUSD M1 datafeed, bid with the close spread
(`dukascopy.py`): the primary training corpus by owner decision 2026-11-28. `dukascopy`
is a converted export read through the generic CSV format. `firstrate-gc` is FirstRate's
ratio-adjusted continuous GC 1-minute series: gold futures trades as a proxy for spot
XAUUSD, kept for reference. Its prices are not tradable spot levels.

`SEALED_FROM` keeps the holdout and the unread slice (protocol v1, #132) out of every
recording, export and statistic (`require_unsealed`). Fixed on 2026-11-28 by the
protocol's length rule (L = 30 months; certified by the agent under owner delegation),
before any statistic read the data. The import may store bars there; nothing reads them
until a recorded step opens the holdout.
"""

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.time import Clock, utc_iso, utc_now
from app.market_data.session import CME_GLOBEX_GC, XAUUSD_NY_1700, SessionSchedule
from app.storage.db import transaction

__all__ = [
    "HOLDOUT",
    "NO_TRADE_FILL",
    "SEALED_FROM",
    "SOURCE_SCHEDULES",
    "UNREAD_SLICE",
    "HoldoutToken",
    "Sealed",
    "SourceConflict",
    "SourceRegistry",
    "UnknownSource",
    "require_unsealed",
]

SOURCE_SCHEDULES: Mapping[str, SessionSchedule] = {
    "dukascopy": XAUUSD_NY_1700,  # [PROPOSED] unverified against Dukascopy XAU/USD data
    "mt5": XAUUSD_NY_1700,  # [PROPOSED] confirm against the broker's symbol spec (#17)
    "firstrate-gc": CME_GLOBEX_GC,  # [PROPOSED] CME Globex hours; no holiday calendar
    "dukascopy-xauusd": XAUUSD_NY_1700,  # [PROPOSED] broker-style; checked by audit on import
}
"""XAUUSD session schedule per data source."""

NO_TRADE_FILL: Mapping[str, timedelta] = {
    "firstrate-gc": timedelta(minutes=15),  # [PROPOSED] check the run lengths on import
    "dukascopy-xauusd": timedelta(minutes=15),  # [PROPOSED] a minute with no tick
}
"""The longest run of open-session minutes with no bar that a source's import fills as
"no trade": a flat bar at the previous close, volume 0. Only a source that omits no-trade
minutes has an entry. A longer run, or any hole in another source, is a real gap."""

# TODO(#132): move HOLDOUT and UNREAD_SLICE into the hashed protocol record once the
# protocol machinery lands; until then this is the one copy the code enforces.
HOLDOUT = (datetime(2024, 5, 28, tzinfo=UTC), datetime(2026, 11, 28, tzinfo=UTC))
"""Protocol v1 holdout, [start, end): 30 months up to 2026-11-28, by the length rule (#132)."""
UNREAD_SLICE = (datetime(2023, 11, 28, tzinfo=UTC), HOLDOUT[0])
"""Protocol v1 unread slice, [start, end): the 6 months before the holdout (#132)."""
SEALED_FROM: Mapping[str, datetime] = {
    "dukascopy-xauusd": UNREAD_SLICE[0],
    "firstrate-gc": UNREAD_SLICE[0],
}
"""Per real-market data source: nothing at or after this instant may be recorded, exported
or read by a statistic until a recorded protocol step opens the holdout. The generic
`dukascopy` CSV key is not sealed (synthetic tests use it), so real data goes through
`dukascopy-xauusd` (and a HistData fallback needs its own sealed key)."""


class HoldoutToken(Protocol):
    """What #132's `RunLog.open_holdout` returns (`HoldoutOpening`)."""

    @property
    def protocol_hash(self) -> str: ...
    @property
    def config_id(self) -> str: ...
    @property
    def opened_at(self) -> datetime: ...


class Sealed(ValueError):
    pass


def require_unsealed(
    data_source: str | None,
    end: datetime,
    *,
    start: datetime | None = None,
    opening: HoldoutToken | None = None,
) -> None:
    """Refuse a window of a sealed data source that ends after its `SEALED_FROM`, unless
    it starts after the holdout (fresh forward data) or the holdout was opened (`opening`).
    Even then the window must end by the holdout's end and, if it has a `start`, must not
    read the unread slice, which v1 never opens. A window with no `start` (the recorder)
    is taken to start at the beginning.

    Callers: `export_dataset`, `MarketRecorder` (its window ends at its clock) and the
    statistics (`era_costs` takes `sealed_from`). TODO(#131): `run_system_backtest` calls
    this with its spec's start and end when #131 merges onto this branch. TODO(#132):
    verify `opening` against `RunLog.holdout_consumed` once the run log lands; until then
    any token with these fields is accepted, so only a real `open_holdout` may make one."""
    sealed = SEALED_FROM.get(data_source) if data_source is not None else None
    if sealed is None or end <= sealed:
        return
    if start is not None and start >= HOLDOUT[1]:
        return  # fresh forward data after both windows: the next claim's evidence
    if opening is None:
        raise Sealed(f"{data_source} is sealed from {sealed.isoformat()} (protocol v1 holdout"
                     " and unread slice, #132): end the window by then")  # fmt: skip
    if end > HOLDOUT[1]:
        raise Sealed(f"the window ends after the holdout ({HOLDOUT[1].isoformat()})")
    if start is not None and start < UNREAD_SLICE[1]:
        raise Sealed("an opened holdout still never reads the unread slice: start the window"
                     f" at {HOLDOUT[0].isoformat()} or later")  # fmt: skip


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

    def require(self, dataset_version: str) -> tuple[str, str]:
        """A bound version's (data source, schedule id); refused if it has no source."""
        row = self._row(dataset_version)
        if row is None:
            raise UnknownSource(f"dataset {dataset_version!r} has no data source")
        return row

    def schedule_of(self, dataset_version: str) -> SessionSchedule:
        """The schedule a dataset version's bars were validated against; refused if the
        version has no source or the code now maps its source to another schedule."""
        row = self.require(dataset_version)
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
