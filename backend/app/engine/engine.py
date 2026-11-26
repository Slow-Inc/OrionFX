"""The M06 engine loop (PRD-06 #16; master plan §4.1-4.5, §9.1-9.2; AT-01..08, AT-11,
AT-12, AT-13, AT-23, AT-32, AT-34, AT-42).

One `tick()` does, in safety-priority order (§4.5):

1. renew the executor lease, or refuse to run (only one writer per account);
2. reconcile when `requires_reconcile` is set (after a restore);
3. control commands from the journal (emergency stop outranks everything);
4. drawdown, independent of any AI input: a breach locks and closes bot exposure;
5. the open cycle: Profit Target and Basket Trailing, then the close workflow;
6. trade intents: every one gets a `risk.decision`; only ALLOWed ones reach the broker.

Journal input is read through the engine's own consumer offset and every handler is
idempotent (keyed on command id / intent id), so a crash between handling and
committing the offset never duplicates work. Start never auto-resumes: a restart
lands in STOPPED (or stays CLOSING / RISK_LOCKED), and the owner must Start again
(AT-07). The engine never touches Manual Positions and never sends an order that
the Risk Veto did not ALLOW; exits are always allowed.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.config import UserConfig, missing_for_start
from app.core.contracts import (
    AccountSnapshot,
    ContractModel,
    ControlCompleted,
    ControlRequested,
    ExecutionUpdated,
    RiskDecision,
    RiskUpdated,
    TradeIntent,
)
from app.core.enums import (
    CloseReason,
    CommandStatus,
    ControlCommand,
    ControlState,
    CycleState,
    DataQuality,
    ExecutionState,
    IntentAction,
    Mode,
    Verdict,
)
from app.core.events import make_event
from app.core.ids import new_id
from app.core.states import ControlStatus, IllegalTransition
from app.core.time import Clock, utc_now
from app.execution.broker import BrokerPort, CloseRequest, OpenRequest, Position
from app.execution.executor import DispatchBlocked, Executor
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry
from app.profit.close import CloseWorkflow
from app.profit.cycle import CycleBook, cycle_net, default_trailing
from app.risk.drawdown import DrawdownTracker
from app.risk.policy import DEFAULT_POLICY, RiskPolicy
from app.risk.veto import EXITS, ConfigEligibility, RiskContext, evaluate
from app.storage.config_store import ConfigStore
from app.storage.flags import REQUIRES_RECONCILE, get_bool_flag, get_flag, set_bool_flag, set_flag
from app.storage.journal import Journal, StoredEvent, payload_as
from app.storage.lease import LeaseStore

LEASE_NAMESPACE = "m06-executor"
CONSUMER = "m06-engine"
CONTROL_FLAG = "m06.control_state"
DEAL_CURSOR_FLAG = "m06.deal_cursor"
SNAPSHOT_EVERY = timedelta(seconds=30)

_S = ControlState
# A restart never resumes trading; pending safety work survives it (§9.1, AT-07).
_ON_RESTART = {
    _S.CLOSING: _S.CLOSING,
    _S.RISK_LOCKED: _S.RISK_LOCKED,
    _S.SAFE_MODE: _S.SAFE_MODE,
}


class LeaseLost(RuntimeError):
    """Another process holds the executor lease; this engine must not write."""


@dataclass(frozen=True)
class EngineSettings:
    account_id: str = "local-account"
    bot_id: str = "bot-1"
    holder: str = field(default_factory=lambda: new_id("engine"))
    lease_ttl: timedelta = timedelta(seconds=30)


class Engine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        broker: BrokerPort,
        *,
        settings: EngineSettings | None = None,
        policy: RiskPolicy = DEFAULT_POLICY,
        clock: Clock = utc_now,
        live_allowed: bool = False,
    ) -> None:
        self._conn = conn
        self._broker = broker
        self._settings = settings or EngineSettings()
        self._policy = policy
        self._clock = clock
        self._live_allowed = live_allowed
        self._journal = Journal(conn)
        self._ledger = ExecutionLedger(conn, clock=clock)
        self._owners = OwnershipRegistry(conn, clock=clock)
        self._lease = LeaseStore(conn, clock=clock)
        self._ex = Executor(conn, broker, clock=clock)
        account = broker.account()
        self._book = CycleBook(conn, account=account, clock=clock)
        self._dd = DrawdownTracker(conn, account=account, clock=clock)
        self._close = CloseWorkflow(conn, broker, self._ex, self._book)
        self._status = ControlStatus(_S.STOPPED, risk_locked=self._dd.state().locked)
        self._emergency_command: str | None = None
        self._last: dict[str, ContractModel] = {}
        self._last_snapshot_at: datetime | None = None

    # --- public -------------------------------------------------------------------------

    @property
    def control(self) -> ControlStatus:
        return self._status

    def start(self) -> bool:
        """Take the lease, reconcile with the broker, restore state without resuming."""
        if not self._renew_lease():
            return False
        saved = get_flag(self._conn, CONTROL_FLAG)
        state = _ON_RESTART.get(_S(saved), _S.STOPPED) if saved else _S.STOPPED
        locked = self._dd.state().locked
        if locked and state is _S.STOPPED:
            state = _S.RISK_LOCKED  # a persisted lock is never shown as a clean stop
        self._status = ControlStatus(state, risk_locked=locked)
        self._persist_state()
        self._reconcile()
        return True

    def tick(self) -> None:
        if not self._renew_lease():
            raise LeaseLost(f"{LEASE_NAMESPACE} is held by {self._lease.holder(LEASE_NAMESPACE)}")
        if get_bool_flag(self._conn, REQUIRES_RECONCILE):
            self._reconcile()
        version, cfg = ConfigStore(self._conn).current()
        events = self._journal.read_since(
            self._journal.committed_offset(CONSUMER),
            event_types=["control.requested", "trade.intent"],
        )
        for ev in events:
            if ev.envelope.event_type == "control.requested":
                self._on_command(ev, cfg, version)
        self._protect(cfg, version)
        for ev in events:
            if ev.envelope.event_type == "trade.intent":
                self._on_intent(ev, cfg)
        if events:
            self._journal.commit_offset(CONSUMER, events[-1].seq)
        self._protect(cfg, version)  # an exit intent may have finished a close
        self._snapshot(cfg)

    # --- infrastructure -----------------------------------------------------------------

    def _renew_lease(self) -> bool:
        return self._lease.acquire(
            LEASE_NAMESPACE, self._settings.holder, ttl=self._settings.lease_ttl
        )

    def _reconcile(self) -> None:
        self._ex.reconcile()
        if not self._ledger.has_unresolved():
            set_bool_flag(self._conn, REQUIRES_RECONCILE, False)

    def _emit(
        self,
        event_type: str,
        payload: ContractModel,
        cfg: UserConfig,
        *,
        cycle_id: str | None = None,
        causation: str | None = None,
    ) -> None:
        self._journal.append(
            make_event(
                event_type,
                source="M06",
                payload=payload,
                account_id=self._settings.account_id,
                bot_id=self._settings.bot_id,
                mode=Mode(cfg.trading.account_mode),
                cycle_id=cycle_id,
                causation_id=causation,
            )
        )

    def _emit_if_changed(
        self, event_type: str, payload: ContractModel, cfg: UserConfig, **kw: str | None
    ) -> None:
        key = f"{event_type}:{kw.get('cycle_id')}"
        if self._last.get(key) != payload:
            self._last[key] = payload
            self._emit(event_type, payload, cfg, **kw)

    def _persist_state(self) -> None:
        set_flag(self._conn, CONTROL_FLAG, self._status.state)

    def _move(self, *path: ControlState) -> None:
        status = self._status
        for target in path:
            status = status.to(target)
        self._status = status
        self._persist_state()

    def _announce(
        self,
        cfg: UserConfig,
        command_id: str,
        status: CommandStatus,
        *,
        reason: str | None = None,
        unresolved: list[str] | None = None,
    ) -> None:
        self._emit(
            "control.completed",
            ControlCompleted(
                command_id=command_id,
                status=status,
                resulting_state=self._status.state,
                risk_locked=self._status.risk_locked,
                unresolved=unresolved or [],
                reason=reason,
            ),
            cfg,
        )

    # --- control commands (§4.5) ----------------------------------------------------------

    def _on_command(self, ev: StoredEvent, cfg: UserConfig, version: int) -> None:
        req = payload_as(ev, ControlRequested)
        if self._journal.find("control.completed", "command_id", req.command_id) is not None:
            return  # already handled before a restart
        handler = {
            ControlCommand.START: self._cmd_start,
            ControlCommand.PAUSE: self._cmd_pause,
            ControlCommand.FINISH_CYCLE: self._cmd_finish,
            ControlCommand.EMERGENCY_STOP: self._cmd_emergency,
            ControlCommand.APPROVE_RECOVERY: self._cmd_recovery,
        }[req.command]
        try:
            handler(req, cfg, version)
        except IllegalTransition as exc:
            self._announce(cfg, req.command_id, CommandStatus.REJECTED, reason=str(exc))

    def _cmd_start(self, req: ControlRequested, cfg: UserConfig, version: int) -> None:
        if missing := missing_for_start(cfg):
            reason = f"config incomplete: {', '.join(missing)}"
            self._announce(cfg, req.command_id, CommandStatus.REJECTED, reason=reason)
            return
        if self._status.risk_locked:
            reason = "risk lock is set: approve recovery first"
            self._announce(cfg, req.command_id, CommandStatus.REJECTED, reason=reason)
            return
        if self._status.state is _S.PAUSED:
            self._move(_S.RUNNING)
        else:
            self._move(_S.STARTING, _S.RECONCILING)
            self._reconcile()
            if self._ledger.has_unresolved():
                self._move(_S.STOPPED)
                unresolved = [r.request_id for r in self._ledger.unresolved()]
                self._announce(
                    cfg,
                    req.command_id,
                    CommandStatus.UNRESOLVED,
                    reason="unresolved requests after reconcile",
                    unresolved=unresolved,
                )
                return
            self._move(_S.RUNNING)
        self._ensure_cycle(cfg, version)
        self._announce(cfg, req.command_id, CommandStatus.COMPLETED)

    def _cmd_pause(self, req: ControlRequested, cfg: UserConfig, version: int) -> None:
        self._move(_S.PAUSED)
        problems = self._close.cancel_bot_orders()  # pending orders would add exposure
        status = CommandStatus.UNRESOLVED if problems else CommandStatus.COMPLETED
        self._announce(cfg, req.command_id, status, unresolved=problems)

    def _cmd_finish(self, req: ControlRequested, cfg: UserConfig, version: int) -> None:
        self._move(_S.FINISHING)
        cycle_id = self._book.open_cycle_id()
        if cycle_id is not None and not self._close.cycle_positions(cycle_id):
            # Nothing to manage: close the empty cycle now (§4.5 "finish, then stop").
            self._book.begin_closing(cycle_id, CloseReason.FINISH_CYCLE)
        self._protect(cfg, version)
        self._announce(cfg, req.command_id, CommandStatus.COMPLETED)

    def _cmd_emergency(self, req: ControlRequested, cfg: UserConfig, version: int) -> None:
        if self._status.state is not _S.CLOSING:
            if self._status.state is _S.STOPPED:
                self._move(_S.STARTING, _S.CLOSING)
            else:
                self._move(_S.CLOSING)
        self._emergency_command = req.command_id
        cycle_id = self._book.open_cycle_id()
        if cycle_id is not None:
            self._book.begin_closing(cycle_id, CloseReason.EMERGENCY_STOP)
        self._protect(cfg, version)

    def _cmd_recovery(self, req: ControlRequested, cfg: UserConfig, version: int) -> None:
        def reject(reason: str) -> None:
            self._announce(cfg, req.command_id, CommandStatus.REJECTED, reason=reason)

        if not self._status.risk_locked:
            return reject("no risk lock to recover from")
        if self._book.open_cycle_id() is not None or self._ledger.has_unresolved():
            return reject("bot exposure or unresolved requests remain")
        equity = self._broker.account().equity
        if not self._dd.approve_recovery(equity, actor=req.actor):
            return reject("lifetime breach cannot be approved away")
        self._status = self._status.unlock()
        if self._status.state is _S.RISK_LOCKED:
            self._move(_S.RECONCILING, _S.STOPPED)
        self._persist_state()
        self._announce(cfg, req.command_id, CommandStatus.COMPLETED)

    # --- protection: drawdown, target, trailing, close ----------------------------------------

    def _ensure_cycle(self, cfg: UserConfig, version: int) -> None:
        target = cfg.trading.profit_target_usd
        if self._book.open_cycle_id() is None and target is not None:
            self._book.open_cycle(target_usd=target, config_version=version)

    def _cash_flow(self) -> Decimal:
        """Deposits/withdrawals since the last tick (AT-23), cursor persisted."""
        cursor = get_flag(self._conn, DEAL_CURSOR_FLAG)
        deals = self._broker.deals_since(cursor)
        if deals:
            set_flag(self._conn, DEAL_CURSOR_FLAG, deals[-1].deal_id)
        return sum((d.profit for d in deals if d.is_balance), Decimal(0))

    def _observe_drawdown(self, cfg: UserConfig) -> None:
        account = self._broker.account()
        owner_max = cfg.trading.max_drawdown_percent or self._policy.max_lifetime_ceiling_percent
        risk = self._dd.observe(
            account.equity, owner_max_dd=owner_max, policy=self._policy, cash_flow=self._cash_flow()
        )
        self._emit_if_changed("risk.updated", risk, cfg)
        if risk.risk_locked and not self._status.risk_locked:
            self._lock(cfg, risk)

    def _lock(self, cfg: UserConfig, risk: RiskUpdated) -> None:
        self._status = self._status.lock()
        if self._status.state not in (_S.STOPPED, _S.RISK_LOCKED):
            self._move(_S.RISK_LOCKED)
        self._persist_state()
        cycle_id = self._book.open_cycle_id()
        if cycle_id is not None:
            self._book.begin_closing(cycle_id, CloseReason.RISK_LIMIT)
        self._announce(
            cfg, new_id("sys"), CommandStatus.COMPLETED, reason=", ".join(risk.reason_codes)
        )

    def _cycle_net(self, cycle_id: str) -> Decimal:
        positions = self._close.cycle_positions(cycle_id)
        floating = sum((p.profit + p.swap for p in positions), Decimal(0))
        return cycle_net(
            realized=self._close.realized_net(cycle_id),
            floating=floating,
            est_close_cost=self._est_close_cost(positions),
        )

    def _est_close_cost(self, positions: list[Position]) -> Decimal:
        """Closing commission assumed equal to the opening one, pro rata to what is
        still open [PROPOSED estimate; the final net always comes from real deals]."""
        if not positions:
            return Decimal(0)
        open_ids = {p.position_id: p.volume for p in positions}
        cost = Decimal(0)
        seen: set[str] = set()
        for d in self._broker.deals_since(None):
            pid = d.position_id
            if pid in open_ids and pid not in seen and d.volume > 0:
                seen.add(pid)
                cost += d.commission * open_ids[pid] / d.volume
        return cost

    def _protect(self, cfg: UserConfig, version: int) -> None:
        self._observe_drawdown(cfg)
        cycle_id = self._book.open_cycle_id()
        if cycle_id is None:
            self._after_cycle(cfg, version)
            return
        snap = self._book.snapshot(cycle_id)
        if snap.state is CycleState.ACTIVE:
            trailing = default_trailing(snap.target_usd, self._policy)
            self._book.evaluate(
                cycle_id, net=self._cycle_net(cycle_id), trailing=trailing, policy=self._policy
            )
        unresolved: list[str] = []
        if self._book.snapshot(cycle_id).state is CycleState.CLOSING:
            unresolved = self._close.run(cycle_id).unresolved
        snap = self._book.snapshot(cycle_id)
        self._emit_if_changed("cycle.updated", snap.as_event(unresolved), cfg, cycle_id=cycle_id)
        if snap.state is CycleState.CLOSED:
            self._after_cycle(cfg, version)
        elif self._emergency_command is not None:
            self._announce(
                cfg, self._emergency_command, CommandStatus.UNRESOLVED, unresolved=unresolved
            )

    def _after_cycle(self, cfg: UserConfig, version: int) -> None:
        """No open cycle: settle the control state that was waiting for it (AT-05)."""
        state = self._status.state
        if state is _S.CLOSING:
            self._move(_S.RISK_LOCKED if self._status.risk_locked else _S.STOPPED)
            if self._emergency_command is not None:
                self._announce(cfg, self._emergency_command, CommandStatus.COMPLETED)
                self._emergency_command = None
        elif state is _S.FINISHING:
            self._move(_S.STOPPED)
        elif state is _S.RUNNING and not self._status.risk_locked:
            self._ensure_cycle(cfg, version)  # normal mode starts the next cycle

    # --- trade intents ------------------------------------------------------------------------

    def _context(self, intent: TradeIntent, cfg: UserConfig) -> RiskContext:
        account = self._broker.account()
        positions = self._broker.positions()
        try:
            spec = self._broker.symbol_spec(intent.symbol)
        except KeyError:
            spec = None
        cycle_id = self._book.open_cycle_id()
        last_net = None if cycle_id is None else self._book.snapshot(cycle_id).last_net_usd
        return RiskContext(
            now=self._clock(),
            mode=Mode(cfg.trading.account_mode),
            live_allowed=self._live_allowed,
            control=self._status,
            account=account,
            spec=spec,
            quote=self._broker.quote(intent.symbol),
            bot_positions=self._owners.bot_positions(account, positions),
            ownership_ambiguous=self._owners.ownership_ambiguous(intent.symbol, positions, account),
            execution_unresolved=self._ledger.has_unresolved(),
            config=cfg,
            policy=self._policy,
            cycle_net_usd=last_net,
            eligibility=ConfigEligibility(cfg, self._policy),
        )

    def _engine_reasons(self, intent: TradeIntent) -> list[str]:
        """Entry checks only the engine can make: cycle and reconcile state."""
        reasons = []
        cycle_id = self._book.open_cycle_id()
        if cycle_id is None or self._book.snapshot(cycle_id).state is not CycleState.ACTIVE:
            reasons.append("CYCLE_NOT_ACTIVE")
        elif intent.cycle_id is not None and intent.cycle_id != cycle_id:
            reasons.append("CYCLE_MISMATCH")
        if get_bool_flag(self._conn, REQUIRES_RECONCILE):
            reasons.append("RECONCILE_REQUIRED")
        return reasons

    def _on_intent(self, ev: StoredEvent, cfg: UserConfig) -> None:
        intent = payload_as(ev, TradeIntent)
        if self._journal.find("risk.decision", "intent_id", intent.intent_id) is not None:
            return  # decided before a restart
        decision = evaluate(intent, self._context(intent, cfg))
        if intent.action not in EXITS and (extra := self._engine_reasons(intent)):
            decision = RiskDecision(
                intent_id=intent.intent_id,
                verdict=Verdict.DENY,
                policy_version=decision.policy_version,
                reason_codes=[*decision.reason_codes, *extra],
                decided_at=decision.decided_at,
            )
        cycle_id = self._book.open_cycle_id()
        self._emit(
            "risk.decision", decision, cfg, cycle_id=cycle_id, causation=ev.envelope.event_id
        )
        if decision.verdict is Verdict.ALLOW:
            for update in self._execute(intent, decision, cycle_id):
                self._emit("execution.updated", update, cfg, cycle_id=cycle_id)

    def _execute(
        self, intent: TradeIntent, decision: RiskDecision, cycle_id: str | None
    ) -> list[ExecutionUpdated]:
        request_id = f"req-{intent.intent_id}"
        try:
            if intent.action in (IntentAction.OPEN, IntentAction.ADD):
                assert intent.side is not None and decision.approved_lot is not None
                return [
                    self._ex.open(
                        OpenRequest(
                            request_id=request_id,
                            symbol=intent.symbol,
                            side=intent.side,
                            volume=decision.approved_lot,
                            cycle_id=cycle_id,
                            intent_id=intent.intent_id,
                        )
                    )
                ]
            return self._exit(intent, decision, request_id, cycle_id)
        except DispatchBlocked as exc:
            return [
                ExecutionUpdated(
                    request_id=request_id,
                    intent_id=intent.intent_id,
                    state=ExecutionState.REJECTED,
                    error=str(exc),
                )
            ]

    def _exit(
        self, intent: TradeIntent, decision: RiskDecision, request_id: str, cycle_id: str | None
    ) -> list[ExecutionUpdated]:
        account = self._broker.account()
        bot = self._owners.bot_positions(account, self._broker.positions())
        if intent.action is IntentAction.CLOSE_ALL:
            targets = bot
        else:
            targets = [p for p in bot if p.position_id in intent.position_refs]
        updates = []
        for n, pos in enumerate(targets):
            volume = decision.approved_lot if len(targets) == 1 and decision.approved_lot else None
            updates.append(
                self._ex.close(
                    CloseRequest(
                        request_id=f"{request_id}-{n}",
                        position_id=pos.position_id,
                        volume=volume or pos.volume,
                        cycle_id=cycle_id,
                    )
                )
            )
        return updates

    # --- snapshots ----------------------------------------------------------------------------

    def _snapshot(self, cfg: UserConfig) -> None:
        now = self._clock()
        if self._last_snapshot_at is not None and now - self._last_snapshot_at < SNAPSHOT_EVERY:
            return
        self._last_snapshot_at = now
        a = self._broker.account()
        quote = self._broker.quote(cfg.trading.symbol)
        fresh = quote is not None and now - quote.time <= timedelta(
            seconds=self._policy.max_quote_age_seconds
        )
        self._emit(
            "account.snapshot",
            AccountSnapshot(
                currency=a.currency,
                balance=a.balance,
                equity=a.equity,
                margin=a.margin,
                free_margin=a.free_margin,
                connected=a.connected,
                quote_status=DataQuality.OK if fresh else DataQuality.STALE,
                observed_at=now,
            ),
            cfg,
        )
