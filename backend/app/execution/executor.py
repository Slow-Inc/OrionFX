"""The single path to the broker (PRD-06 #13).

Order of operations per write: refuse if the request id was seen or any outcome
is unknown -> record PENDING (committed) -> send -> record the outcome and link
ownership. `reconcile()` resolves unknown outcomes from broker deals; it never
re-sends.
"""

import sqlite3
from decimal import Decimal

from app.core.contracts import ExecutionUpdated
from app.core.enums import ExecutionState
from app.execution.broker import BrokerPort, CloseRequest, OpenRequest, SendResult, SendStatus
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry


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
    def __init__(self, conn: sqlite3.Connection, broker: BrokerPort) -> None:
        self._ledger = ExecutionLedger(conn)
        self._owners = OwnershipRegistry(conn)
        self._broker = broker

    def _guard(self, request_id: str) -> None:
        if self._ledger.exists(request_id):
            raise DispatchBlocked(f"request {request_id} was already recorded")
        if self._ledger.unresolved():
            raise DispatchBlocked("an earlier request has an unknown outcome; reconcile first")

    def _record(
        self,
        request_id: str,
        intent_id: str | None,
        result: SendResult,
        state: ExecutionState,
    ) -> ExecutionUpdated:
        self._ledger.update(
            request_id,
            state,
            filled_volume=result.filled_volume if result.status is SendStatus.ACCEPTED else None,
            fill_price=result.price,
            broker_position_id=result.position_id,
            broker_deal_ids=result.deal_ids,
            error=result.error,
        )
        return ExecutionUpdated(
            request_id=request_id,
            intent_id=intent_id,
            state=state,
            broker_position_id=result.position_id,
            broker_deal_ids=list(result.deal_ids),
            filled_lot=result.filled_volume if result.status is SendStatus.ACCEPTED else None,
            fill_price=result.price,
            error=result.error,
        )

    def open(self, req: OpenRequest) -> ExecutionUpdated:
        self._guard(req.request_id)
        self._ledger.create(
            request_id=req.request_id,
            kind="OPEN",
            requested_volume=req.volume,
            intent_id=req.intent_id,
            cycle_id=req.cycle_id,
            symbol=req.symbol,
            side=req.side,
        )
        result = self._broker.send_open(req)
        state = _state(result, req.volume)
        if result.status is SendStatus.ACCEPTED and result.position_id:
            self._owners.link(result.position_id, req.request_id, req.cycle_id, req.symbol)
        return self._record(req.request_id, req.intent_id, result, state)

    def close(self, req: CloseRequest) -> ExecutionUpdated:
        position = next(
            (p for p in self._broker.positions() if p.position_id == req.position_id), None
        )
        if position is None or not self._owners.is_bot(position):
            raise DispatchBlocked(f"{req.position_id} is not a bot position")
        self._guard(req.request_id)
        self._ledger.create(
            request_id=req.request_id,
            kind="CLOSE",
            requested_volume=req.volume,
            cycle_id=req.cycle_id,
            symbol=position.symbol,
            position_id=req.position_id,
        )
        result = self._broker.send_close(req)
        return self._record(req.request_id, None, result, _state(result, req.volume))

    def cancel(self, order_id: str, request_id: str) -> ExecutionUpdated:
        self._guard(request_id)
        self._ledger.create(request_id=request_id, kind="CANCEL", requested_volume=Decimal(0))
        result = self._broker.cancel_order(order_id, request_id)
        state = (
            ExecutionState.CANCELLED
            if result.status is SendStatus.ACCEPTED
            else _state(result, Decimal(0))
        )
        return self._record(request_id, None, result, state)

    def reconcile(self) -> list[ExecutionUpdated]:
        """Resolve PENDING/UNKNOWN requests from broker deals. Never re-sends."""
        updates: list[ExecutionUpdated] = []
        for row in self._ledger.unresolved():
            deals = self._broker.find_by_request(row.request_id)
            if not deals:
                result = SendResult(SendStatus.REJECTED, error="no broker record: not executed")
                updates.append(
                    self._record(row.request_id, row.intent_id, result, ExecutionState.CANCELLED)
                )
                continue
            filled = sum((d.volume for d in deals), Decimal(0))
            position_id = deals[0].position_id
            result = SendResult(
                SendStatus.ACCEPTED,
                position_id=position_id,
                deal_ids=tuple(d.deal_id for d in deals),
                filled_volume=filled,
                price=deals[-1].price,
            )
            if row.kind == "OPEN" and position_id and row.symbol:
                self._owners.link(position_id, row.request_id, row.cycle_id, row.symbol)
            updates.append(
                self._record(
                    row.request_id, row.intent_id, result, _state(result, row.requested_volume)
                )
            )
        return updates
