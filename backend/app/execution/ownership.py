"""Which broker positions and orders are the bot's (TRD-04, master plan §4.4, AT-06, AT-08).

A position is the bot's only when its magic number matches AND it is linked, on
this account/server and symbol, to a request this engine recorded. The magic
number alone is never sufficient.
"""

import sqlite3
from collections.abc import Iterable

from app.core.time import Clock, utc_now
from app.execution.broker import BOT_MAGIC, AccountInfo, MarginMode, PendingOrder, Position
from app.storage.db import transaction


class OwnershipRegistry:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def link(
        self,
        account: AccountInfo,
        position_id: str,
        *,
        request_id: str,
        cycle_id: str | None,
        symbol: str,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO bot_positions (position_id, login, server, symbol, request_id,"
                " cycle_id, linked_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(login, server, position_id) DO NOTHING",
                (
                    position_id,
                    account.login,
                    account.server,
                    symbol,
                    request_id,
                    cycle_id,
                    self._clock().isoformat(),
                ),
            )

    def link_order(self, account: AccountInfo, order_id: str, *, request_id: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO bot_orders (order_id, login, server, request_id, linked_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(login, server, order_id) DO NOTHING",
                (order_id, account.login, account.server, request_id, self._clock().isoformat()),
            )

    def is_linked(self, account: AccountInfo, position_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM bot_positions WHERE login = ? AND server = ? AND position_id = ?",
            (account.login, account.server, position_id),
        ).fetchone()
        return row is not None

    def is_bot(self, account: AccountInfo, position: Position) -> bool:
        if position.magic != BOT_MAGIC:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM bot_positions"
            " WHERE login = ? AND server = ? AND position_id = ? AND symbol = ?",
            (account.login, account.server, position.position_id, position.symbol),
        ).fetchone()
        return row is not None

    def is_bot_order(self, account: AccountInfo, order: PendingOrder) -> bool:
        if order.magic != BOT_MAGIC:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM bot_orders WHERE login = ? AND server = ? AND order_id = ?",
            (account.login, account.server, order.order_id),
        ).fetchone()
        return row is not None

    def bot_positions(self, account: AccountInfo, positions: Iterable[Position]) -> list[Position]:
        return [p for p in positions if self.is_bot(account, p)]

    def cycle_of(self, account: AccountInfo, position_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT cycle_id FROM bot_positions WHERE login = ? AND server = ? AND position_id = ?",
            (account.login, account.server, position_id),
        ).fetchone()
        return None if row is None else row[0]

    def positions_of_cycle(self, account: AccountInfo, cycle_id: str) -> set[tuple[str, str]]:
        """(position_id, symbol) pairs the bot opened for this cycle."""
        rows = self._conn.execute(
            "SELECT position_id, symbol FROM bot_positions"
            " WHERE login = ? AND server = ? AND cycle_id = ?",
            (account.login, account.server, cycle_id),
        ).fetchall()
        return {(r[0], r[1]) for r in rows}

    def ownership_ambiguous(
        self, symbol: str, positions: Iterable[Position], account: AccountInfo
    ) -> bool:
        """On a netting account any non-bot position on the symbol would merge with,
        or be reduced by, a bot order; MVP blocks that case instead of guessing (§4.4)."""
        if account.margin_mode is not MarginMode.NETTING:
            return False
        return any(p.symbol == symbol and not self.is_bot(account, p) for p in positions)
