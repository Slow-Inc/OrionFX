"""Close workflow for a CLOSING cycle (PRD-06 #15; master plan §4.1 steps 3-5; AT-01,
AT-04, AT-11; CAT-R01-05).

One pass: reconcile unknown outcomes first (never re-send blind), then send one
close per remaining bot position of the cycle. Any rejection, unknown outcome or
blocked dispatch is reported as unresolved and the pass stops short of claiming
success. The cycle becomes CLOSED only when the broker shows no bot position of
the cycle and no request is unresolved; the final net comes from actual deals.
"""

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.enums import CycleState, ExecutionState
from app.core.ids import new_id
from app.execution.broker import BrokerPort, CloseRequest, Position
from app.execution.executor import DispatchBlocked, Executor
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry
from app.profit.cycle import CycleBook


@dataclass(frozen=True)
class CloseReport:
    closed: bool
    unresolved: list[str] = field(default_factory=list)


class CloseWorkflow:
    def __init__(
        self, conn: sqlite3.Connection, broker: BrokerPort, executor: Executor, book: CycleBook
    ) -> None:
        self._conn = conn
        self._broker = broker
        self._ex = executor
        self._book = book
        self._owners = OwnershipRegistry(conn)
        self._ledger = ExecutionLedger(conn)

    def _cycle_positions(self, cycle_id: str) -> list[Position]:
        account = self._broker.account()
        return [
            p
            for p in self._owners.bot_positions(account, self._broker.positions())
            if self._owners.cycle_of(account, p.position_id) == cycle_id
        ]

    def realized_net(self, cycle_id: str) -> Decimal:
        account = self._broker.account()
        total = Decimal(0)
        for d in self._broker.deals_since(None):
            if d.is_balance or d.position_id is None:
                continue
            if self._owners.cycle_of(account, d.position_id) == cycle_id:
                total += d.profit - d.commission + d.swap
        return total

    def run(self, cycle_id: str) -> CloseReport:
        state = self._book.snapshot(cycle_id).state
        if state is CycleState.CLOSED:
            return CloseReport(True)  # idempotent: the engine may call again after a close
        if state is not CycleState.CLOSING:
            raise ValueError("close workflow runs only on a CLOSING cycle")
        self._ex.reconcile()
        unresolved = [f"request {r.request_id}: outcome unknown" for r in self._ledger.unresolved()]
        if unresolved:
            return CloseReport(False, unresolved)

        for pos in self._cycle_positions(cycle_id):
            try:
                upd = self._ex.close(
                    CloseRequest(
                        request_id=new_id("close"),
                        position_id=pos.position_id,
                        volume=pos.volume,
                        cycle_id=cycle_id,
                    )
                )
            except DispatchBlocked as exc:
                unresolved.append(f"{pos.position_id}: {exc}")
                break  # an unknown outcome blocks every further write until reconciled
            if upd.state is not ExecutionState.FILLED:
                unresolved.append(f"{pos.position_id}: {upd.state} {upd.error or ''}".strip())

        if unresolved or self._ledger.has_unresolved() or self._cycle_positions(cycle_id):
            left = [p.position_id for p in self._cycle_positions(cycle_id)]
            return CloseReport(False, unresolved or [f"{pid}: still open" for pid in left])
        self._book.mark_closed(cycle_id, final_net=self.realized_net(cycle_id))
        return CloseReport(True)
