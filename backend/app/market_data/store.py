"""Point-in-time candle store (#32; DAT-04, AT-15).

Each bar is stored once per dataset version with the time it became available.
`bars(..., as_of=T)` returns only bars available at T, oldest first, so a
backtest asks "what did we know at T" and gets exactly that. Stored bars are
immutable (storage triggers forbid UPDATE and DELETE): saving the identical bar
again is a no-op, a different bar for the same key is refused; a new dataset
version is the way to correct data.
"""

import sqlite3
from datetime import UTC, datetime

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.storage.db import transaction


class CandleConflict(ValueError):
    pass


def _ts(t: datetime) -> str:
    """UTC ISO text sorts in time order; every stored and queried time is UTC."""
    if t.tzinfo is None:
        raise ValueError("naive datetime: pass an aware UTC time")
    return t.astimezone(UTC).isoformat()


class CandleStore:
    def __init__(self, conn: sqlite3.Connection, *, dataset_version: str) -> None:
        self.conn = conn
        self.dataset_version = dataset_version

    def key(self, bar: CandleClosed) -> tuple[str, str, str, str]:
        """A bar's identity: the store's primary key and the candle event id."""
        return (self.dataset_version, bar.symbol, bar.timeframe.value, _ts(bar.open_time))

    def event_id(self, bar: CandleClosed) -> str:
        return "candle:" + ":".join(self.key(bar))

    def save(self, bar: CandleClosed) -> None:
        key = self.key(bar)
        body = bar.model_dump_json()
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO candles (dataset_version, symbol, timeframe, open_time,"
                " available_at, body) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*key, _ts(bar.available_at), body),
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
                _ts(as_of),
                "" if after is None else _ts(after),
                -1 if limit is None else limit,
            ),
        ).fetchall()
        return [CandleClosed.model_validate_json(r[0]) for r in reversed(rows)]

    def bar_at(
        self, symbol: str, timeframe: Timeframe, open_time: datetime, *, as_of: datetime
    ) -> CandleClosed | None:
        """The bar opening at `open_time`, if it was available at `as_of`."""
        row = self.conn.execute(
            "SELECT body FROM candles WHERE dataset_version = ? AND symbol = ?"
            " AND timeframe = ? AND open_time = ? AND available_at <= ?",
            (self.dataset_version, symbol, timeframe.value, _ts(open_time), _ts(as_of)),
        ).fetchone()
        return None if row is None else CandleClosed.model_validate_json(row[0])
