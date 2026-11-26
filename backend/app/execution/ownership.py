"""Which broker positions are the bot's (TRD-04, master plan §4.4, AT-06, AT-08).

A position is the bot's only when its magic number matches AND it is linked to a
request this engine recorded. The magic number alone is never sufficient.
"""

import sqlite3
from collections.abc import Iterable

from app.core.time import utc_now_iso
from app.execution.broker import BOT_MAGIC, AccountInfo, MarginMode, Position
from app.storage.db import transaction


class OwnershipRegistry:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def link(self, position_id: str, request_id: str, cycle_id: str | None, symbol: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO bot_positions (position_id, request_id, cycle_id, symbol, linked_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(position_id) DO NOTHING",
                (position_id, request_id, cycle_id, symbol, utc_now_iso()),
            )

    def _linked(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT position_id FROM bot_positions")}

    def is_bot(self, position: Position) -> bool:
        return position.magic == BOT_MAGIC and position.position_id in self._linked()

    def bot_positions(self, positions: Iterable[Position]) -> list[Position]:
        linked = self._linked()
        return [p for p in positions if p.magic == BOT_MAGIC and p.position_id in linked]

    def cycle_of(self, position_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT cycle_id FROM bot_positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return None if row is None else row[0]

    def ownership_ambiguous(
        self, symbol: str, positions: Iterable[Position], account: AccountInfo
    ) -> bool:
        """On a netting account a manual position on the symbol would merge with the
        bot's; MVP blocks that case instead of guessing (§4.4)."""
        if account.margin_mode is not MarginMode.NETTING:
            return False
        linked = self._linked()
        return any(
            p.symbol == symbol and not (p.magic == BOT_MAGIC and p.position_id in linked)
            for p in positions
        )
