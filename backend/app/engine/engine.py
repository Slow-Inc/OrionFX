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

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError

from app.core.config import UserConfig, missing_for_start
from app.core.contracts import (
    AccountSnapshot,
    BotPosition,
    ContractModel,
    ControlCompleted,
    ControlRequested,
    ExecutionUpdated,
    PositionsSnapshot,
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
from app.execution.broker import AccountInfo, BrokerPort, CloseRequest, OpenRequest, Position
from app.execution.executor import DispatchBlocked, Executor
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry
from app.profit.close import CloseWorkflow
from app.profit.cycle import CycleBook, cycle_net, default_trailing
from app.risk.drawdown import DrawdownTracker
from app.risk.policy import DEFAULT_POLICY, RiskPolicy
from app.risk.veto import EXITS, ConfigEligibility, RiskContext, evaluate, quote_fresh
from app.storage.config_store import ConfigStore
from app.storage.db import transaction
from app.storage.flags import REQUIRES_RECONCILE, get_bool_flag, get_flag, set_bool_flag, set_flag
from app.storage.journal import Journal, StoredEvent, payload_as
from app.storage.lease import LeaseStore

LEASE_NAMESPACE = "m06-executor"
CONSUMER = "m06-engine"
CONTROL_FLAG = "m06.control_state"
DEAL_CURSOR_FLAG = "m06.deal_cursor"
EMERGENCY_FLAG = "m06.emergency_command"
FINISH_FLAG = "m06.finish_command"

log = logging.getLogger(__name__)
SNAPSHOT_EVERY = timedelta(seconds=30)

_S = ControlState
# A restart never resumes trading; pending safety work survives it (§9.1, AT-07).
_KEPT_ON_RESTART = frozenset({_S.CLOSING, _S.RISK_LOCKED})


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
        state = _S(saved) if saved in _KEPT_ON_RESTART else _S.STOPPED
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
        # Commands first (they outrank intents); offsets are committed only after
        # every event of the batch, so a command is never skipped past an intent.
        for ev in events:
            if ev.envelope.event_type == "control.requested":
                self._on_command(ev, cfg, version)
        self._protect(cfg, version)
        executed = False
        for ev in events:
            if ev.envelope.event_type == "trade.intent":
                executed |= self._on_intent_safely(ev, cfg)
        if events:
            self._journal.commit_offset(CONSUMER, events[-1].seq)
        if executed:
            self._protect(cfg, version)  # an exit may have emptied the cycle
        self._snapshot(cfg, force=executed)

    # --- infrastructure -----------------------------------------------------------------

    def _pending_emergency(self) -> str | None:
        """The Emergency Stop still being worked on; persisted so a restart finishes it."""
        return get_flag(self._conn, EMERGENCY_FLAG) or None

    def _set_pending_emergency(self, command_id: str | None) -> None:
        set_flag(self._conn, EMERGENCY_FLAG, command_id or "")

    def _pending_finish(self) -> str | None:
        return get_flag(self._conn, FINISH_FLAG) or None

    def _set_pending_finish(self, command_id: str | None) -> None:
        set_flag(self._conn, FINISH_FLAG, command_id or "")

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
                occurred_at=self._clock(),
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
        self._set_pending_finish(req.command_id)
        self._announce(cfg, req.command_id, CommandStatus.ACCEPTED)
        self._protect(cfg, version)  # completes it at once when nothing is open

    def _cmd_emergency(self, req: ControlRequested, cfg: UserConfig, version: int) -> None:
        if self._status.state is not _S.CLOSING:
            if self._status.state is _S.STOPPED:
                self._move(_S.STARTING, _S.CLOSING)
            else:
                self._move(_S.CLOSING)
        self._set_pending_emergency(req.command_id)
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
        self._announce(cfg, req.command_id, CommandStatus.COMPLETED)

    # --- protection: drawdown, target, trailing, close ----------------------------------------

    def _ensure_cycle(self, cfg: UserConfig, version: int) -> None:
        target = cfg.trading.profit_target_usd
        if self._book.open_cycle_id() is None and target is not None:
            self._book.open_cycle(target_usd=target, config_version=version)

    def _cash_flow(self) -> tuple[Decimal, str | None]:
        """Deposits/withdrawals since the cursor (AT-23), and the new cursor."""
        deals = self._broker.deals_since(get_flag(self._conn, DEAL_CURSOR_FLAG))
        amount = sum((d.profit for d in deals if d.is_balance), Decimal(0))
        return amount, deals[-1].deal_id if deals else None

    def _observe_drawdown(self, cfg: UserConfig) -> None:
        account = self._broker.account()
        owner_max = cfg.trading.max_drawdown_percent or self._policy.max_lifetime_ceiling_percent
        cash_flow, cursor = self._cash_flow()
        with transaction(self._conn):  # a failed update must not consume the cash flow
            risk = self._dd.observe(
                account.equity, owner_max_dd=owner_max, policy=self._policy, cash_flow=cash_flow
            )
            if cursor is not None:
                set_flag(self._conn, DEAL_CURSOR_FLAG, cursor)
        self._emit_if_changed("risk.updated", risk, cfg)
        if risk.risk_locked and not self._status.risk_locked:
            self._lock(cfg, risk)

    def _lock(self, cfg: UserConfig, risk: RiskUpdated) -> None:
        self._status = self._status.lock()
        if self._status.state not in (_S.STOPPED, _S.RISK_LOCKED, _S.CLOSING):
            self._move(_S.RISK_LOCKED)
        cycle_id = self._book.open_cycle_id()
        if cycle_id is not None:
            self._book.begin_closing(cycle_id, CloseReason.RISK_LIMIT)
        self._announce(
            cfg, new_id("sys"), CommandStatus.COMPLETED, reason=", ".join(risk.reason_codes)
        )

    def _cycle_net(self, cycle_id: str) -> Decimal:
        realized, floating, est_close = self._close.net_inputs(cycle_id)
        return cycle_net(realized=realized, floating=floating, est_close_cost=est_close)

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
        elif (command_id := self._pending_emergency()) is not None:
            last = self._journal.find("control.completed", "command_id", command_id)
            said = None if last is None else payload_as(last, ControlCompleted)
            if said is None or said.unresolved != unresolved:
                self._announce(cfg, command_id, CommandStatus.UNRESOLVED, unresolved=unresolved)

    def _after_cycle(self, cfg: UserConfig, version: int) -> None:
        """No open cycle: settle the control state that was waiting for it (AT-05)."""
        state = self._status.state
        if state is _S.CLOSING:
            self._move(_S.RISK_LOCKED if self._status.risk_locked else _S.STOPPED)
        if state is _S.FINISHING:
            self._move(_S.STOPPED)
        if (command_id := self._pending_emergency()) is not None:
            self._announce(cfg, command_id, CommandStatus.COMPLETED)
            self._set_pending_emergency(None)
        if (command_id := self._pending_finish()) is not None and state in (
            _S.FINISHING,
            _S.CLOSING,
        ):
            self._announce(cfg, command_id, CommandStatus.COMPLETED)
            self._set_pending_finish(None)
        if state is _S.RUNNING and not self._status.risk_locked:
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

    def _exit_targets(self, intent: TradeIntent) -> list[Position]:
        bot = self._owners.bot_positions(self._broker.account(), self._broker.positions())
        bot = [p for p in bot if p.symbol == intent.symbol]
        if intent.action is IntentAction.CLOSE_ALL:
            return bot
        return [p for p in bot if p.position_id in intent.position_refs]

    def _engine_reasons(self, intent: TradeIntent, cfg: UserConfig) -> list[str]:
        """Checks only the engine can make: mode, cycle, basket, reconcile state, exit
        targets. An entry (OPEN/ADD) is refused in Shadow (no broker order, master plan
        §13), when it names no cycle or another cycle, when an OPEN would start a second
        basket, or when an ADD is on the other side of the bot's basket."""
        if intent.action in EXITS:
            return [] if self._exit_targets(intent) else ["NOTHING_TO_CLOSE"]
        reasons = []
        if Mode(cfg.trading.account_mode) is Mode.SHADOW:
            reasons.append("SHADOW_NO_ORDER")
        cycle_id = self._book.open_cycle_id()
        if cycle_id is None or self._book.snapshot(cycle_id).state is not CycleState.ACTIVE:
            reasons.append("CYCLE_NOT_ACTIVE")
        elif intent.cycle_id != cycle_id:
            reasons.append("CYCLE_MISMATCH")
        held = self._owners.bot_positions(self._broker.account(), self._broker.positions())
        held = [p for p in held if p.symbol == intent.symbol]
        if intent.action is IntentAction.OPEN and held:
            reasons.append("POSITION_OPEN")
        if intent.action is IntentAction.ADD and any(p.side != intent.side for p in held):
            reasons.append("SIDE_MISMATCH")
        if get_bool_flag(self._conn, REQUIRES_RECONCILE):
            reasons.append("RECONCILE_REQUIRED")
        return reasons

    def _on_intent_safely(self, ev: StoredEvent, cfg: UserConfig) -> bool:
        """One failing intent is reported and never blocks the ones behind it."""
        try:
            return self._on_intent(ev, cfg)
        except Exception as exc:
            log.exception("intent handling failed")
            intent = payload_as(ev, TradeIntent)
            self._fail_intent(intent, cfg, f"engine error: {exc!r}")
            return True  # something may have reached the broker: protect again

    def _fail_intent(self, intent: TradeIntent, cfg: UserConfig, error: str) -> None:
        if self._journal.find("risk.decision", "intent_id", intent.intent_id) is None:
            self._emit(
                "risk.decision",
                RiskDecision(
                    intent_id=intent.intent_id,
                    verdict=Verdict.DENY,
                    policy_version=self._policy.policy_version,
                    reason_codes=["ENGINE_ERROR"],
                    decided_at=self._clock(),
                ),
                cfg,
            )
        elif self._journal.find("execution.updated", "intent_id", intent.intent_id) is None:
            self._emit(
                "execution.updated",
                ExecutionUpdated(
                    request_id=f"req-{intent.intent_id}",
                    intent_id=intent.intent_id,
                    state=ExecutionState.REJECTED,
                    error=error,
                ),
                cfg,
            )

    def _dispatched(self, intent_id: str) -> bool:
        base = f"req-{intent_id}"
        return any(self._ledger.state(r) is not None for r in (base, f"{base}-0"))

    def _on_intent(self, ev: StoredEvent, cfg: UserConfig) -> bool:
        """True when the intent reached the broker."""
        intent = payload_as(ev, TradeIntent)
        decided = self._journal.find("risk.decision", "intent_id", intent.intent_id)
        if decided is not None:
            # Decided before a restart. If it was ALLOWed but never dispatched, say so;
            # it is not sent late on an old decision (reconcile covers what was sent).
            allowed = payload_as(decided, RiskDecision).verdict is Verdict.ALLOW
            if allowed and not self._dispatched(intent.intent_id):
                self._fail_intent(intent, cfg, "interrupted before dispatch; not retried")
            return False
        decision = evaluate(intent, self._context(intent, cfg))
        if extra := self._engine_reasons(intent, cfg):
            decision = decision.model_copy(
                update={
                    "verdict": Verdict.DENY,
                    "approved_lot": None,
                    "valid_until": None,
                    "reason_codes": [*decision.reason_codes, *extra],
                }
            )
        cycle_id = self._book.open_cycle_id()
        self._emit(
            "risk.decision", decision, cfg, cycle_id=cycle_id, causation=ev.envelope.event_id
        )
        if decision.verdict is not Verdict.ALLOW:
            return False
        for update in self._execute(intent, decision, cycle_id):
            self._emit("execution.updated", update, cfg, cycle_id=cycle_id)
        return True

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
        # A requested lot caps the total closed; without one, each target closes fully.
        budget = decision.approved_lot if intent.requested_lot is not None else None
        updates = []
        for n, pos in enumerate(self._exit_targets(intent)):
            volume = pos.volume if budget is None else min(budget, pos.volume)
            if volume <= 0:
                break
            rid = f"{request_id}-{n}"
            try:
                update = self._ex.close(
                    CloseRequest(
                        request_id=rid,
                        position_id=pos.position_id,
                        volume=volume,
                        cycle_id=cycle_id,
                    )
                )
            except DispatchBlocked as exc:  # e.g. an Emergency SL closed it meanwhile
                update = ExecutionUpdated(
                    request_id=rid,
                    intent_id=intent.intent_id,
                    state=ExecutionState.REJECTED,
                    error=str(exc),
                )
            updates.append(update)
            if budget is not None:
                budget -= volume
            if self._ledger.has_unresolved():
                break  # an unknown outcome blocks every further write until reconciled
        return updates

    # --- snapshots ----------------------------------------------------------------------------

    def _snapshot(self, cfg: UserConfig, *, force: bool = False) -> None:
        """Every SNAPSHOT_EVERY, and at once after an execution (`force`), so positions
        that just opened or closed are never shown from an older reading."""
        now = self._clock()
        recent = (
            self._last_snapshot_at is not None and now - self._last_snapshot_at < SNAPSHOT_EVERY
        )
        if recent and not force:
            return
        self._last_snapshot_at = now
        a = self._broker.account()
        quote = self._broker.quote(cfg.trading.symbol)
        fresh = quote is not None and quote_fresh(quote, now, self._policy)
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
        self._positions_snapshot(cfg, a, now)

    def _positions_snapshot(self, cfg: UserConfig, a: AccountInfo, now: datetime) -> None:
        """The bot's own positions, by the same ownership rule M06 acts on (same reading as
        account.snapshot). Nothing is published from a disconnected terminal, or when a
        position cannot be read cleanly: the dashboard's list then turns stale instead of
        showing a wrong list as fresh."""
        if not a.connected:
            return
        mine = self._owners.bot_positions_with_cycle(a, self._broker.positions())
        try:
            items = [
                BotPosition(
                    position_id=p.position_id, symbol=p.symbol, side=p.side, volume=p.volume,
                    open_price=p.open_price, profit=p.profit, commission=p.commission,
                    swap=p.swap, cycle_id=cycle_id,
                )
                for p, cycle_id in mine
            ]  # fmt: skip
        except ValidationError:
            return
        self._emit("positions.snapshot", PositionsSnapshot(items=items, observed_at=now), cfg)
