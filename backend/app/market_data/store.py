"""Point-in-time candle store (#32; DAT-04, AT-15).

Each bar is stored once per dataset version with the time it became available.
`bars(..., as_of=T)` returns only bars available at T, oldest first, so a
backtest asks "what did we know at T" and gets exactly that. Stored bars are
immutable: saving the identical bar again is a no-op, a different bar for the
same key is refused (a new dataset version is the way to correct data).
"""

import sqlite3
from datetime import datetime

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.storage.db import transaction


class CandleConflict(ValueError):
    pass


def _ts(t: datetime) -> str:
    """UTC ISO text sorts in time order (every stored time is UTC)."""
    return t.isoformat()


class CandleStore:
    def __init__(self, conn: sqlite3.Connection, *, dataset_version: str) -> None:
        self._conn = conn
        self.dataset_version = dataset_version

    def save(self, bar: CandleClosed) -> bool:
        """True when stored, False when the identical bar was already there."""
        key = (self.dataset_version, bar.symbol, bar.timeframe.value, _ts(bar.open_time))
        body = bar.model_dump_json()
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT body FROM candles WHERE dataset_version = ? AND symbol = ?"
                " AND timeframe = ? AND open_time = ?",
                key,
            ).fetchone()
            if row is not None:
                if CandleClosed.model_validate_json(row[0]) != bar:
                    raise CandleConflict(f"{key} is stored with different content")
                return False
            self._conn.execute(
                "INSERT INTO candles (dataset_version, symbol, timeframe, open_time,"
                " available_at, body) VALUES (?, ?, ?, ?, ?, ?)",
                (*key, _ts(bar.available_at), body),
            )
        return True

    def bars(
        self, symbol: str, timeframe: Timeframe, *, as_of: datetime, limit: int | None = None
    ) -> list[CandleClosed]:
        """Bars available at `as_of`, oldest first (the newest `limit` if given)."""
        rows = self._conn.execute(
            "SELECT body FROM candles WHERE dataset_version = ? AND symbol = ?"
            " AND timeframe = ? AND available_at <= ? ORDER BY open_time DESC LIMIT ?",
            (self.dataset_version, symbol, timeframe.value, _ts(as_of), limit or -1),
        ).fetchall()
        return [CandleClosed.model_validate_json(r[0]) for r in reversed(rows)]
