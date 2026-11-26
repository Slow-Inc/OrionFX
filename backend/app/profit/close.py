"""Close workflow for a CLOSING cycle (PRD-06 #15; master plan §4.1 steps 3-5; AT-01,
AT-04, AT-11; CAT-R01-05).

One pass: reconcile unknown outcomes first (never re-send blind), cancel bot
pending orders, then send one close per remaining bot position of the cycle. Any
rejection, unknown outcome or blocked dispatch is reported as unresolved and the
pass stops short of claiming success. The cycle becomes CLOSED only when the
broker shows no bot position of the cycle, no bot pending order and no
unresolved request; the final net comes from actual deals.
"""

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.enums import CycleState, ExecutionState
from app.core.ids import new_id
from app.execution.broker import BOT_MAGIC, BrokerPort, CloseRequest, PendingOrder, Position
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

    def cycle_positions(self, cycle_id: str) -> list[Position]:
        owned = self._owners.positions_of_cycle(self._broker.account(), cycle_id)
        return [
            p
            for p in self._broker.positions()
            if p.magic == BOT_MAGIC and (p.position_id, p.symbol) in owned
        ]

    def _bot_orders(self) -> list[PendingOrder]:
        """Only one cycle is open per account, so every bot pending order is this cycle's."""
        account = self._broker.account()
        return [o for o in self._broker.pending_orders() if self._owners.is_bot_order(account, o)]

    def cancel_bot_orders(self) -> list[str]:
        problems: list[str] = []
        for order in self._bot_orders():
            try:
                upd = self._ex.cancel(order.order_id, new_id("cancel"))
            except DispatchBlocked as exc:
                problems.append(f"{order.order_id}: {exc}")
                continue
            if upd.state is not ExecutionState.CANCELLED:
                problems.append(f"{order.order_id}: {upd.state} {upd.error or ''}".strip())
        return problems

    def net_inputs(self, cycle_id: str) -> tuple[Decimal, Decimal, Decimal]:
        """(realized, floating, estimated close cost) from one deal read. The close
        commission is assumed equal to the opening one, pro rata to what is still
        open [PROPOSED estimate; the final net always comes from real deals]."""
        account = self._broker.account()
        ids = {pid for pid, _ in self._owners.positions_of_cycle(account, cycle_id)}
        open_volume = {p.position_id: p.volume for p in self.cycle_positions(cycle_id)}
        realized = floating = est_close = Decimal(0)
        opened: set[str] = set()
        for d in self._broker.deals_since(None):
            if d.is_balance or d.position_id not in ids:
                continue
            realized += d.profit - d.commission + d.swap
            pid = d.position_id
            if pid in open_volume and pid not in opened and d.volume > 0:
                opened.add(pid)
                est_close += d.commission * open_volume[pid] / d.volume
        for p in self._broker.positions():
            if p.position_id in open_volume:
                floating += p.profit + p.swap
        return realized, floating, est_close

    def realized_net(self, cycle_id: str) -> Decimal:
        ids = {pid for pid, _ in self._owners.positions_of_cycle(self._broker.account(), cycle_id)}
        return sum(
            (
                d.profit - d.commission + d.swap
                for d in self._broker.deals_since(None)
                if not d.is_balance and d.position_id in ids
            ),
            Decimal(0),
        )

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

        unresolved += self.cancel_bot_orders()
        for pos in self.cycle_positions(cycle_id):
            if self._ledger.has_unresolved():
                unresolved.append(f"{pos.position_id}: waiting for reconcile")
                break  # an unknown outcome blocks every further write until reconciled
            try:
                upd = self._ex.close(
                    CloseRequest(
                        request_id=new_id("close"),
                        position_id=pos.position_id,
                        volume=pos.volume,
                        cycle_id=cycle_id,
                    )
                )
            except DispatchBlocked as exc:  # e.g. an Emergency SL closed it meanwhile
                unresolved.append(f"{pos.position_id}: {exc}")
                continue
            if upd.state is not ExecutionState.FILLED:
                unresolved.append(f"{pos.position_id}: {upd.state} {upd.error or ''}".strip())

        remaining = self.cycle_positions(cycle_id)
        if unresolved or self._ledger.has_unresolved() or remaining or self._bot_orders():
            left = [f"{p.position_id}: still open" for p in remaining]
            return CloseReport(False, unresolved or left)
        self._book.mark_closed(cycle_id, final_net=self.realized_net(cycle_id))
        return CloseReport(True)
