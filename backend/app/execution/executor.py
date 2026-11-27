"""The single path to the broker (PRD-06 #13).

Per write: refuse while any outcome is unresolved, refuse ambiguous or foreign
targets -> record PENDING (committed; the primary key rejects a reused request
id) -> send -> record the outcome and link ownership in one transaction. If the
broker call raises, the row is recorded UNKNOWN: the order may have left.

`reconcile()` resolves unresolved requests from broker state and never re-sends.
An empty broker lookup is not proof that nothing happened (the order may still
be in flight), so a request is only written off as CANCELLED after a quiet
period with the broker connected.
"""

import sqlite3
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from app.core.contracts import ExecutionUpdated
from app.core.enums import ExecutionState, RequestKind
from app.core.time import Clock, utc_now
from app.execution.broker import (
    AccountInfo,
    BrokerPort,
    CloseRequest,
    OpenRequest,
    SendResult,
    SendStatus,
)
from app.execution.ledger import DuplicateRequest, ExecutionLedger, LedgerRow
from app.execution.ownership import OwnershipRegistry
from app.storage.db import transaction

RECONCILE_QUIET_PERIOD = timedelta(minutes=2)
"""[PROPOSED] how long a request with no broker trace stays UNKNOWN before it is
written off. Broker-specific; to be measured on a real terminal (#17)."""


class DispatchBlocked(Exception):
    pass


def _state(result: SendResult, requested: Decimal) -> ExecutionState:
    if result.status is SendStatus.REJECTED:
        return ExecutionState.REJECTED
    if result.status is SendStatus.UNKNOWN:
        return ExecutionState.UNKNOWN
    if result.filled_volume < requested:
        return ExecutionState.PARTIALLY_FILLED
    return ExecutionState.FILLED


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        broker: BrokerPort,
        *,
        clock: Clock = utc_now,
        quiet_period: timedelta = RECONCILE_QUIET_PERIOD,
    ) -> None:
        self._conn = conn
        self._ledger = ExecutionLedger(conn, clock=clock)
        self._owners = OwnershipRegistry(conn, clock=clock)
        self._broker = broker
        self._clock = clock
        self._quiet = quiet_period

    # --- write path --------------------------------------------------------------------

    def _begin(self, **row: object) -> None:
        if self._ledger.has_unresolved():
            raise DispatchBlocked("an earlier request has an unknown outcome; reconcile first")
        try:
            self._ledger.create(**row)  # type: ignore[arg-type]
        except DuplicateRequest as exc:
            raise DispatchBlocked(str(exc)) from exc

    @staticmethod
    def _send(call: Callable[[], SendResult]) -> SendResult:
        try:
            return call()
        except Exception as exc:
            return SendResult(SendStatus.UNKNOWN, error=f"broker call raised: {exc!r}")

    def _record(
        self,
        request_id: str,
        intent_id: str | None,
        state: ExecutionState,
        *,
        filled: Decimal | None = None,
        price: Decimal | None = None,
        position_id: str | None = None,
        deal_ids: tuple[str, ...] = (),
        error: str | None = None,
    ) -> ExecutionUpdated:
        self._ledger.update(
            request_id,
            state,
            filled_volume=filled,
            fill_price=price,
            broker_position_id=position_id,
            broker_deal_ids=deal_ids,
            error=error,
        )
        return ExecutionUpdated(
            request_id=request_id,
            intent_id=intent_id,
            state=state,
            broker_position_id=position_id,
            broker_deal_ids=list(deal_ids),
            filled_lot=filled,
            fill_price=price,
            error=error,
        )

    def _record_result(
        self, request_id: str, intent_id: str | None, result: SendResult, requested: Decimal
    ) -> ExecutionUpdated:
        accepted = result.status is SendStatus.ACCEPTED
        return self._record(
            request_id,
            intent_id,
            _state(result, requested),
            filled=result.filled_volume if accepted else None,
            price=result.price,
            position_id=result.position_id,
            deal_ids=result.deal_ids,
            error=result.error,
        )

    def open(self, req: OpenRequest) -> ExecutionUpdated:
        account = self._broker.account()
        before = self._broker.positions()
        if self._owners.ownership_ambiguous(req.symbol, before, account):
            raise DispatchBlocked(
                f"netting account holds a non-bot position on {req.symbol}: ownership ambiguous"
            )
        foreign = {
            p.position_id for p in before if not self._owners.is_linked(account, p.position_id)
        }
        self._begin(
            request_id=req.request_id,
            kind=RequestKind.OPEN,
            requested_volume=req.volume,
            intent_id=req.intent_id,
            cycle_id=req.cycle_id,
            symbol=req.symbol,
            side=req.side,
        )
        result = self._send(lambda: self._broker.send_open(req))
        with transaction(self._conn):
            # Never adopt a position that existed before this order and was not ours.
            if (
                result.status is SendStatus.ACCEPTED
                and result.position_id
                and result.position_id not in foreign
            ):
                self._owners.link(
                    account,
                    result.position_id,
                    request_id=req.request_id,
                    cycle_id=req.cycle_id,
                    symbol=req.symbol,
                )
            return self._record_result(req.request_id, req.intent_id, result, req.volume)

    def close(self, req: CloseRequest) -> ExecutionUpdated:
        account = self._broker.account()
        position = next(
            (p for p in self._broker.positions() if p.position_id == req.position_id), None
        )
        if position is None or not self._owners.is_bot(account, position):
            raise DispatchBlocked(f"{req.position_id} is not a bot position")
        if req.volume > position.volume:
            raise DispatchBlocked("close volume exceeds the position (would reverse on netting)")
        self._begin(
            request_id=req.request_id,
            kind=RequestKind.CLOSE,
            requested_volume=req.volume,
            cycle_id=req.cycle_id,
            symbol=position.symbol,
            position_id=req.position_id,
        )
        result = self._send(lambda: self._broker.send_close(req))
        return self._record_result(req.request_id, None, result, req.volume)

    def cancel(self, order_id: str, request_id: str) -> ExecutionUpdated:
        account = self._broker.account()
        order = next((o for o in self._broker.pending_orders() if o.order_id == order_id), None)
        if order is None or not self._owners.is_bot_order(account, order):
            raise DispatchBlocked(f"{order_id} is not a bot order")
        self._begin(
            request_id=request_id,
            kind=RequestKind.CANCEL,
            requested_volume=Decimal(0),
            symbol=order.symbol,
            order_id=order_id,
        )
        result = self._send(lambda: self._broker.cancel_order(order_id, request_id))
        if result.status is SendStatus.ACCEPTED:
            return self._record(request_id, None, ExecutionState.CANCELLED)
        return self._record_result(request_id, None, result, Decimal(0))

    # --- reconciliation -------------------------------------------------------------------

    def reconcile(self) -> list[ExecutionUpdated]:
        """Resolve PENDING/UNKNOWN requests from broker state. Never re-sends."""
        account = self._broker.account()
        updates: list[ExecutionUpdated] = []
        for row in self._ledger.unresolved():
            if row.kind is RequestKind.CANCEL:
                update = self._reconcile_cancel(row)
            else:
                update = self._reconcile_trade(row, account)
            if update is not None:
                updates.append(update)
        return updates

    def _reconcile_cancel(self, row: LedgerRow) -> ExecutionUpdated | None:
        # A cancel leaves no deal: the order being gone is the evidence.
        order_id = self._ledger.order_of(row.request_id)
        still_there = any(o.order_id == order_id for o in self._broker.pending_orders())
        if still_there:
            return self._record(
                row.request_id, None, ExecutionState.REJECTED, error="order still pending"
            )
        return self._record(row.request_id, None, ExecutionState.CANCELLED)

    def _reconcile_trade(self, row: LedgerRow, account: AccountInfo) -> ExecutionUpdated | None:
        deals = self._broker.find_by_request(row.request_id)
        if not deals:
            if self._clock() - row.created_at >= self._quiet and account.connected:
                return self._record(
                    row.request_id,
                    row.intent_id,
                    ExecutionState.CANCELLED,
                    error="no broker trace after the quiet period: not executed",
                )
            return None  # too early to tell: stays unresolved, dispatch stays blocked
        filled = sum((d.volume for d in deals), Decimal(0))
        priced = [(d.volume, d.price) for d in deals if d.price is not None]
        priced_volume = sum((v for v, _ in priced), Decimal(0))
        vwap = (
            sum((v * p for v, p in priced), Decimal(0)) / priced_volume if priced_volume else None
        )
        position_id = deals[0].position_id
        state = (
            ExecutionState.FILLED
            if filled >= row.requested_volume
            else ExecutionState.PARTIALLY_FILLED
        )
        with transaction(self._conn):
            if row.kind is RequestKind.OPEN and position_id and row.symbol:
                self._owners.link(
                    account,
                    position_id,
                    request_id=row.request_id,
                    cycle_id=row.cycle_id,
                    symbol=row.symbol,
                )
            return self._record(
                row.request_id,
                row.intent_id,
                state,
                filled=filled,
                price=vwap,
                position_id=position_id,
                deal_ids=tuple(d.deal_id for d in deals),
            )
