"""Point-in-time candle store (#32; DAT-04, AT-15).

Each bar is stored once per dataset version with the time it became available.
`bars(..., as_of=T)` returns only bars available at T, oldest first, so a
backtest asks "what did we know at T" and gets exactly that. Stored bars are
immutable (storage triggers forbid UPDATE and DELETE): saving the identical bar
again is a no-op, a different bar for the same key is refused; a new dataset
version is the way to correct data.
"""

import sqlite3
from datetime import datetime

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.core.time import utc_iso
from app.storage.db import transaction


class CandleConflict(ValueError):
    pass


class CandleStore:
    def __init__(self, conn: sqlite3.Connection, *, dataset_version: str) -> None:
        self.conn = conn
        self.dataset_version = dataset_version

    def key(self, bar: CandleClosed) -> tuple[str, str, str, str]:
        """A bar's identity: the store's primary key and the candle event id."""
        return (self.dataset_version, bar.symbol, bar.timeframe.value, utc_iso(bar.open_time))

    def event_id(self, bar: CandleClosed) -> str:
        return "candle:" + ":".join(self.key(bar))

    def save(self, bar: CandleClosed) -> None:
        key = self.key(bar)
        body = bar.model_dump_json()
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO candles (dataset_version, symbol, timeframe, open_time,"
                " available_at, body) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*key, utc_iso(bar.available_at), body),
            )
            if cur.rowcount == 1:
                return
            (stored,) = self.conn.execute(
                "SELECT body FROM candles WHERE dataset_version = ? AND symbol = ?"
                " AND timeframe = ? AND open_time = ?",
                key,
            ).fetchone()
            if stored != body and CandleClosed.model_validate_json(stored) != bar:
                raise CandleConflict(f"{key} is stored with different content")

    def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        as_of: datetime,
        limit: int | None = None,
        after: datetime | None = None,
    ) -> list[CandleClosed]:
        """Bars available at `as_of` (and, given `after`, only those that became available
        after it), oldest first (the newest `limit` if given)."""
        rows = self.conn.execute(
            "SELECT body FROM candles WHERE dataset_version = ? AND symbol = ?"
            " AND timeframe = ? AND available_at <= ? AND available_at > ?"
            " ORDER BY available_at DESC LIMIT ?",
            (
                self.dataset_version,
                symbol,
                timeframe.value,
                utc_iso(as_of),
                "" if after is None else utc_iso(after),
                -1 if limit is None else limit,
            ),
        ).fetchall()
        return [CandleClosed.model_validate_json(r[0]) for r in reversed(rows)]

    def latest_open(self, symbol: str, timeframe: Timeframe, *, as_of: datetime) -> datetime | None:
        """The open time of the newest `timeframe` bar available at `as_of` (indexed:
        `available_at` rises with `open_time` within a timeframe)."""
        row = self.conn.execute(
            "SELECT open_time FROM candles WHERE dataset_version = ? AND symbol = ?"
            " AND timeframe = ? AND available_at <= ? ORDER BY available_at DESC LIMIT 1",
            (self.dataset_version, symbol, timeframe.value, utc_iso(as_of)),
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row[0])

    def bar_at(
        self, symbol: str, timeframe: Timeframe, open_time: datetime, *, as_of: datetime
    ) -> CandleClosed | None:
        """The bar opening at `open_time`, if it was available at `as_of`."""
        row = self.conn.execute(
            "SELECT body FROM candles WHERE dataset_version = ? AND symbol = ?"
            " AND timeframe = ? AND open_time = ? AND available_at <= ?",
            (self.dataset_version, symbol, timeframe.value, utc_iso(open_time), utc_iso(as_of)),
        ).fetchone()
        return None if row is None else CandleClosed.model_validate_json(row[0])
