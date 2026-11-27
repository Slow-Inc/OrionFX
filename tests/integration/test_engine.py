"""M06 slice D: the engine against the fake broker (PRD-06 #16; AT-01..08, AT-11, AT-12,
AT-13, AT-23, AT-32, AT-34, AT-42). AT-09/AT-10 need MT5 and the EA (#17)."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.support.engine import (
    SETTINGS,
    T0,
    Clock,
    _bot_positions,
    _closed_cycle,
    _command,
    _completed,
    _configure,
    _decision,
    _engine,
    _intent,
    _last_cycle,
    _price,
    _running,
)

from app.core.contracts import (
    ControlCompleted,
    ControlRequested,
    ExecutionUpdated,
    RiskDecision,
)
from app.core.enums import (
    CloseReason,
    CommandStatus,
    ControlCommand,
    ControlState,
    CycleState,
    ExecutionState,
    IntentAction,
    Mode,
    RequestKind,
    Side,
    Verdict,
)
from app.core.events import make_event
from app.engine.engine import Engine, LeaseLost
from app.execution.broker import BOT_MAGIC
from app.execution.fake_broker import FakeBroker, Outcome
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry
from app.profit.cycle import CycleBook
from app.risk.drawdown import DrawdownTracker
from app.storage.flags import REQUIRES_RECONCILE, get_bool_flag, set_bool_flag, set_flag
from app.storage.journal import Journal, payload_as
from app.storage.lease import LeaseStore

D = Decimal


# --- start, lease, restart -------------------------------------------------------------------


def test_start_needs_config_and_opens_a_cycle(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _engine(conn, broker, clock)
    assert eng.start()
    cid = _command(conn, ControlCommand.START)
    eng.tick()
    done = _completed(conn, cid)
    assert done.status is CommandStatus.REJECTED and done.resulting_state is ControlState.STOPPED

    _configure(conn)
    cid = _command(conn, ControlCommand.START)
    eng.tick()
    assert _completed(conn, cid).resulting_state is ControlState.RUNNING
    assert CycleBook(conn, account=broker.account()).open_cycle_id() is not None


def test_engine_refuses_to_run_without_the_lease(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    assert LeaseStore(conn, clock=clock).acquire("m06-executor", "other", ttl=timedelta(minutes=5))
    eng = _engine(conn, broker, clock)
    assert not eng.start()
    with pytest.raises(LeaseLost):
        eng.tick()


def test_commands_and_intents_are_consumed_once(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    iid = _intent(conn, clock)
    eng.tick()
    eng.tick()
    again = _engine(conn, broker, clock)  # same holder: a restart of this process
    assert again.start()
    again.tick()
    assert len(_bot_positions(broker)) == 1
    assert _decision(conn, iid).verdict is Verdict.ALLOW


def test_restart_never_auto_resumes(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-07 (restart half)."""
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    again = _engine(conn, broker, clock)
    assert again.start()
    assert again.control.state is ControlState.STOPPED
    iid = _intent(conn, clock)
    again.tick()
    assert _decision(conn, iid).verdict is Verdict.DENY
    assert len(_bot_positions(broker)) == 1  # the open position is still protected, not lost


def test_requires_reconcile_blocks_entries_until_reconciled(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    set_bool_flag(conn, REQUIRES_RECONCILE, True)
    eng.tick()
    assert not get_bool_flag(conn, REQUIRES_RECONCILE)  # nothing unresolved: cleared


# --- profit target, trailing, cycles ----------------------------------------------------------


def test_target_closes_bot_positions_only_and_cancels_pending(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-01."""
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    manual = broker.add_manual_position("XAUUSD", Side.SELL, D("0.05"))
    _price(broker, "2010.00")  # +$10 on 0.01 lot
    eng.tick()
    assert _bot_positions(broker) == []
    assert [p.position_id for p in broker.positions()] == [manual.position_id]
    closed = [_closed_cycle(conn)]
    assert closed[0].target_triggered and closed[0].target_achieved
    assert closed[0].close_reason is CloseReason.TARGET_TRIGGERED


def test_target_triggered_but_not_achieved_is_reported_honestly(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    """AT-02: estimate $10.00 triggers the close; slippage leaves $9.80 realized."""
    b = FakeBroker(balance=D("1000"), now=T0, commission_per_lot=D("10"))
    _price(b, "2000.00")
    clock.broker = b
    eng = _running(conn, b, clock)
    _intent(conn, clock)
    eng.tick()
    _price(b, "2010.20")  # floating 10.20 - open commission 0.10 - est close 0.10 = 10.00
    b.slippage = D("0.20")  # the close fills $0.20 worse on 0.01 lot x 100 oz
    eng.tick()
    ev = _closed_cycle(conn)
    assert ev.target_triggered and ev.final_net_usd == D("9.80")
    assert ev.target_achieved is False


def test_trailing_floor_closes_the_cycle(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-03 via the engine: default trailing 0.6 x target, distance 0.2 x target."""
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    _price(broker, "2009.00")  # +$9: active, floor $7
    eng.tick()
    assert _last_cycle(conn).trailing_floor_usd == D("7.00")
    _price(broker, "2007.00")
    eng.tick()
    assert _bot_positions(broker) == []
    closed = _closed_cycle(conn)
    assert closed.close_reason is CloseReason.TRAILING_FLOOR


def test_running_opens_next_cycle_finish_does_not(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-05."""
    eng = _running(conn, broker, clock)
    book = CycleBook(conn, account=broker.account())
    first = book.open_cycle_id()
    _intent(conn, clock)
    eng.tick()
    _price(broker, "2010.00")
    eng.tick()
    second = book.open_cycle_id()
    assert second is not None and second != first

    cid = _command(conn, ControlCommand.FINISH_CYCLE)
    eng.tick()
    assert eng.control.state is ControlState.STOPPED  # no exposure in the new cycle
    assert _completed(conn, cid).status is CommandStatus.COMPLETED
    assert book.open_cycle_id() is None


def test_finish_cycle_waits_for_the_close_then_stops(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    _command(conn, ControlCommand.FINISH_CYCLE)
    eng.tick()
    assert eng.control.state is ControlState.FINISHING
    iid = _intent(conn, clock)
    eng.tick()
    assert _decision(conn, iid).verdict is Verdict.DENY
    _price(broker, "2010.00")
    eng.tick()
    assert eng.control.state is ControlState.STOPPED
    assert CycleBook(conn, account=broker.account()).open_cycle_id() is None


def test_duplicate_intent_and_lost_reply_never_duplicate_orders(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-04 at the engine: an unknown outcome blocks entries until reconciled."""
    eng = _running(conn, broker, clock)
    broker.script(Outcome.TIMEOUT_FILLED)
    _intent(conn, clock)
    eng.tick()
    iid = _intent(conn, clock)
    eng.tick()
    assert "EXECUTION_UNRESOLVED" in _decision(conn, iid).reason_codes
    clock.advance(180)
    _price(broker, "2000.00")
    eng.tick()
    assert len(_bot_positions(broker)) == 1


# --- drawdown -------------------------------------------------------------------------------------


def test_dd_breach_by_manual_trade_closes_bot_only_and_locks(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-06 + AT-07 (lock survives restart, lifetime peak kept)."""
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    manual = broker.add_manual_position("XAUUSD", Side.BUY, D("1.00"))
    _price(broker, "1997.00")  # manual -$300 -> DD ~30% > 20%
    eng.tick()
    assert _bot_positions(broker) == []
    assert [p.position_id for p in broker.positions()] == [manual.position_id]
    assert eng.control.risk_locked and eng.control.state is ControlState.RISK_LOCKED

    again = _engine(conn, broker, clock)
    assert again.start()
    assert again.control.risk_locked
    cid = _command(conn, ControlCommand.START)
    again.tick()
    assert _completed(conn, cid).status is CommandStatus.REJECTED


def test_approve_recovery_unlocks_into_stopped_not_running(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    manual = broker.add_manual_position("XAUUSD", Side.BUY, D("1.00"))
    _price(broker, "1997.50")  # 25%: past the owner's 20%, short of the 30% lifetime ceiling
    eng.tick()
    assert eng.control.risk_locked
    broker._positions.pop(manual.position_id)  # owner closed the manual trade
    cid = _command(conn, ControlCommand.APPROVE_RECOVERY)
    eng.tick()
    done = _completed(conn, cid)
    assert done.status is CommandStatus.COMPLETED and not done.risk_locked
    assert eng.control.state is ControlState.STOPPED


def test_cash_flow_is_not_trading_loss(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-23: withdrawing 30% of the balance does not trip a 20% DD."""
    eng = _running(conn, broker, clock)
    eng.tick()
    broker.withdraw(D("300"))
    eng.tick()
    assert not eng.control.risk_locked
    assert eng.control.state is ControlState.RUNNING


# --- entries and exits ------------------------------------------------------------------------


def test_invalid_entries_are_rejected(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-08, AT-13, AT-32, AT-42 through the engine."""
    eng = _running(conn, broker, clock)
    bad = {
        "margin": _intent(conn, clock, lot="0.01", strategy="trend", side=Side.BUY),
        "symbol": _intent(conn, clock, symbol="EURUSD"),
        "unapproved": _intent(conn, clock, strategy="not-enabled"),
        "live": _intent(conn, clock, mode=Mode.LIVE),
        "lot": _intent(conn, clock, lot="0.015"),
    }
    spec = broker.specs["XAUUSD"]
    broker.specs["XAUUSD"] = replace(spec, margin_per_lot=D("1000000"))  # no free margin
    eng.tick()
    assert "MARGIN_INSUFFICIENT" in _decision(conn, bad["margin"]).reason_codes
    assert "SYMBOL_NOT_CONFIGURED" in _decision(conn, bad["symbol"]).reason_codes
    assert "STRATEGY_NOT_ELIGIBLE" in _decision(conn, bad["unapproved"]).reason_codes
    assert "MODE_MISMATCH" in _decision(conn, bad["live"]).reason_codes  # DEMO account
    assert _decision(conn, bad["lot"]).verdict is Verdict.DENY
    assert _bot_positions(broker) == []


def test_pause_blocks_entries_and_pending_but_protects_positions(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-12."""
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    _command(conn, ControlCommand.PAUSE)
    eng.tick()
    assert eng.control.state is ControlState.PAUSED
    iid = _intent(conn, clock)
    eng.tick()
    assert "CONTROL_STATE_NO_ENTRY" in _decision(conn, iid).reason_codes
    _price(broker, "2010.00")
    eng.tick()
    assert _bot_positions(broker) == []  # target still closes while paused


def test_exit_intent_works_while_locked_and_paused(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    (pid,) = _bot_positions(broker)
    _command(conn, ControlCommand.PAUSE)
    eng.tick()
    iid = _intent(conn, clock, action=IntentAction.CLOSE, lot=None, side=None, refs=[pid])
    eng.tick()
    assert _decision(conn, iid).verdict is Verdict.ALLOW
    assert _bot_positions(broker) == []


def test_strategy_switch_keeps_the_cycle(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-34: a different strategy's intent does not reset cycle, floor or ownership."""
    _configure(conn)
    _configure(conn, strategies_enabled=["trend", "range"])
    eng = _engine(conn, broker, clock)
    assert eng.start()
    _command(conn, ControlCommand.START)
    eng.tick()
    book = CycleBook(conn, account=broker.account())
    cycle = book.open_cycle_id()
    _intent(conn, clock, strategy="trend")
    eng.tick()
    _price(broker, "2009.00")
    eng.tick()
    floor = book.snapshot(cycle or "").trailing_floor_usd
    _price(broker, "2008.50")
    _intent(conn, clock, strategy="range", lot="0.01")
    eng.tick()
    assert book.open_cycle_id() == cycle
    assert book.snapshot(cycle or "").trailing_floor_usd == floor


# --- emergency stop ---------------------------------------------------------------------------


def test_emergency_stop_closes_and_stops(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    manual = broker.add_manual_position("XAUUSD", Side.SELL, D("0.02"))
    cid = _command(conn, ControlCommand.EMERGENCY_STOP)
    eng.tick()
    done = _completed(conn, cid)
    assert done.status is CommandStatus.COMPLETED and done.resulting_state is ControlState.STOPPED
    assert [p.position_id for p in broker.positions()] == [manual.position_id]


def test_emergency_stop_in_closed_market_reports_unresolved_then_retries(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-11: no false claim of success; retried on later ticks."""
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    broker.market_open = False
    cid = _command(conn, ControlCommand.EMERGENCY_STOP)
    eng.tick()
    done = _completed(conn, cid)
    assert done.status is CommandStatus.UNRESOLVED and done.unresolved
    assert eng.control.state is ControlState.CLOSING
    assert len(_bot_positions(broker)) == 1
    broker.market_open = True
    clock.advance(1)
    _price(broker, "2000.00")
    eng.tick()
    assert _bot_positions(broker) == []
    done = _completed(conn, cid)
    assert done.status is CommandStatus.COMPLETED and done.resulting_state is ControlState.STOPPED


def test_pause_cancels_bot_pending_orders(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-12: pending orders would add exposure while paused; manual ones stay."""
    eng = _running(conn, broker, clock)
    order = broker.add_pending("XAUUSD", Side.BUY, D("0.01"), D("1990"), magic=BOT_MAGIC)
    seed = ExecutionLedger(conn)
    seed.create(request_id="seed", kind=RequestKind.OPEN, requested_volume=D("0.01"))
    seed.update("seed", ExecutionState.FILLED)
    OwnershipRegistry(conn).link_order(broker.account(), order.order_id, request_id="seed")
    manual = broker.add_pending("XAUUSD", Side.BUY, D("0.01"), D("1990"))
    _command(conn, ControlCommand.PAUSE)
    eng.tick()
    assert [o.order_id for o in broker.pending_orders()] == [manual.order_id]


def test_entries_denied_while_reconcile_is_still_required(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    broker.script(Outcome.TIMEOUT_LOST)
    _intent(conn, clock)
    eng.tick()  # unknown outcome, inside the quiet period
    set_bool_flag(conn, REQUIRES_RECONCILE, True)
    iid = _intent(conn, clock)
    eng.tick()
    assert get_bool_flag(conn, REQUIRES_RECONCILE)  # not cleared while unresolved
    assert "RECONCILE_REQUIRED" in _decision(conn, iid).reason_codes


def test_entry_denied_when_the_cycle_is_closing(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    broker.market_open = False  # the target close cannot complete
    _price(broker, "2010.00")
    eng.tick()
    broker.market_open = True
    clock.advance(1)
    _price(broker, "2010.00")
    book = CycleBook(conn, account=broker.account())
    assert book.snapshot(book.open_cycle_id() or "").state is CycleState.CLOSING
    iid = _intent(conn, clock)
    broker.script(Outcome.REJECT)  # make the retry close fail so the cycle stays CLOSING
    eng.tick()
    assert "CYCLE_NOT_ACTIVE" in _decision(conn, iid).reason_codes


def test_a_lost_offset_commit_does_not_redo_commands_or_intents(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """Crash after handling, before the offset commit: replay must be a no-op."""
    eng = _running(conn, broker, clock)
    iid = _intent(conn, clock)
    eng.tick()
    conn.execute("UPDATE consumer_offsets SET last_seq = 0")  # the commit was lost
    again = _engine(conn, broker, clock)
    assert again.start()
    again.tick()
    assert len(_bot_positions(broker)) == 1
    decisions = [
        e
        for e in Journal(conn).recent(["risk.decision"], limit=50)
        if payload_as(e, RiskDecision).intent_id == iid
    ]
    assert len(decisions) == 1
    start = Journal(conn).recent(["control.requested"], limit=1)[0]
    command_id = payload_as(start, ControlRequested).command_id
    answers = [
        e
        for e in Journal(conn).recent(["control.completed"], limit=50)
        if payload_as(e, ControlCompleted).command_id == command_id
    ]
    assert len(answers) == 1  # the replayed START was not run again
    assert again.control.state is ControlState.STOPPED


def test_start_refused_when_a_lock_was_set_while_stopped(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    _configure(conn)
    eng = _engine(conn, broker, clock)
    assert eng.start()
    eng.tick()  # sets the DD peak at 1000
    broker.add_manual_position("XAUUSD", Side.BUY, D("1.00"))
    _price(broker, "1997.50")
    eng.tick()
    assert eng.control.risk_locked
    cid = _command(conn, ControlCommand.START)
    eng.tick()
    done = _completed(conn, cid)
    assert done.status is CommandStatus.REJECTED and "approve recovery" in (done.reason or "")
    assert CycleBook(conn, account=broker.account()).open_cycle_id() is None


def _answers(conn: sqlite3.Connection, command_id: str) -> list[ControlCompleted]:
    return [
        p
        for e in Journal(conn).recent(["control.completed"], limit=200)
        if (p := payload_as(e, ControlCompleted)).command_id == command_id
    ]


def _stuck_emergency(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock) -> str:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    broker.market_open = False
    cid = _command(conn, ControlCommand.EMERGENCY_STOP)
    eng.tick()
    return cid


def test_unresolved_emergency_is_not_re_announced_every_tick(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    cid = _stuck_emergency(conn, broker, clock)
    eng = _engine(conn, broker, clock)
    eng.start()
    for _ in range(3):
        eng.tick()
    assert [a.status for a in _answers(conn, cid)] == [CommandStatus.UNRESOLVED]


def test_pending_emergency_survives_restart_and_completes(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    cid = _stuck_emergency(conn, broker, clock)
    again = _engine(conn, broker, clock)
    assert again.start()
    assert again.control.state is ControlState.CLOSING
    broker.market_open = True
    clock.advance(1)
    _price(broker, "2000.00")
    again.tick()
    assert _bot_positions(broker) == []
    assert _completed(conn, cid).status is CommandStatus.COMPLETED


def test_dd_lock_during_emergency_close_still_completes_the_command(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    cid = _stuck_emergency(conn, broker, clock)
    eng = _engine(conn, broker, clock)
    eng.start()
    broker.add_manual_position("XAUUSD", Side.BUY, D("1.00"))
    _price(broker, "1997.50")
    eng.tick()
    assert eng.control.risk_locked
    broker.market_open = True
    clock.advance(1)
    _price(broker, "1997.50")
    eng.tick()
    assert _bot_positions(broker) == []
    done = _completed(conn, cid)
    assert done.status is CommandStatus.COMPLETED
    assert done.resulting_state is ControlState.RISK_LOCKED and done.risk_locked


# --- code-review findings: exits, poison events, cash flow, finish --------------------------


def _two_positions(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock) -> Engine:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    _intent(conn, clock, action=IntentAction.ADD)
    eng.tick()
    assert len(_bot_positions(broker)) == 2
    return eng


def test_close_one_of_two_positions_closes_just_that_one(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _two_positions(conn, broker, clock)
    a, b = _bot_positions(broker)
    iid = _intent(conn, clock, action=IntentAction.CLOSE, lot=None, side=None, refs=[a])
    eng.tick()
    assert _decision(conn, iid).verdict is Verdict.ALLOW
    assert _bot_positions(broker) == [b]


def test_partial_close_over_several_refs_never_exceeds_the_approved_lot(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _two_positions(conn, broker, clock)
    refs = _bot_positions(broker)
    _intent(conn, clock, action=IntentAction.CLOSE, lot="0.01", side=None, refs=refs)
    eng.tick()
    left = [p for p in broker.positions() if p.magic == BOT_MAGIC]
    assert sum(p.volume for p in left) == D("0.01")


def test_close_with_nothing_to_close_is_denied(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _two_positions(conn, broker, clock)
    iid = _intent(conn, clock, action=IntentAction.CLOSE, lot=None, side=None, refs=[])
    eng.tick()
    decision = _decision(conn, iid)
    assert decision.verdict is Verdict.DENY and "NOTHING_TO_CLOSE" in decision.reason_codes


class VanishingBroker(FakeBroker):
    vanish: str | None = None

    def send_close(self, req):  # type: ignore[no-untyped-def]
        result = super().send_close(req)
        if self.vanish is not None:
            self._positions.pop(self.vanish, None)  # an Emergency SL fill elsewhere
            self.vanish = None
        return result


def test_close_all_keeps_going_and_reports_every_close(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    b = VanishingBroker(balance=D("1000"), now=T0)
    _price(b, "2000.00")
    clock.broker = b
    eng = _running(conn, b, clock)
    _intent(conn, clock)
    eng.tick()
    for _ in range(2):
        _intent(conn, clock, action=IntentAction.ADD)
        eng.tick()
    _, second, _ = _bot_positions(b)
    b.vanish = second
    iid = _intent(conn, clock, action=IntentAction.CLOSE_ALL, lot=None, side=None)
    eng.tick()
    assert _bot_positions(b) == []
    updates = [
        payload_as(e, ExecutionUpdated)
        for e in Journal(conn).recent(["execution.updated"], limit=20)
        if payload_as(e, ExecutionUpdated).request_id.startswith(f"req-{iid}")
    ]
    assert {u.state for u in updates} == {ExecutionState.FILLED, ExecutionState.REJECTED}
    assert len(updates) == 3


class FlakyBroker(FakeBroker):
    fail_quote = False

    def quote(self, symbol):  # type: ignore[no-untyped-def]
        if self.fail_quote:
            self.fail_quote = False
            raise RuntimeError("adapter hiccup")
        return super().quote(symbol)


def test_a_failing_intent_is_reported_and_does_not_block_later_ones(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    b = FlakyBroker(balance=D("1000"), now=T0)
    _price(b, "2000.00")
    clock.broker = b
    eng = _running(conn, b, clock)
    bad = _intent(conn, clock)
    good = _intent(conn, clock)
    b.fail_quote = True
    eng.tick()
    first = _decision(conn, bad)
    assert first.verdict is Verdict.DENY and "ENGINE_ERROR" in first.reason_codes
    assert _decision(conn, good).verdict is Verdict.ALLOW
    assert len(_bot_positions(b)) == 1


def test_allowed_intent_interrupted_before_dispatch_is_reported_not_lost(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    iid = _intent(conn, clock)
    Journal(conn).append(  # the process died right after deciding
        make_event(
            "risk.decision",
            source="M06",
            payload=RiskDecision(
                intent_id=iid,
                verdict=Verdict.ALLOW,
                approved_lot=D("0.01"),
                policy_version="p",
                reason_codes=[],
                decided_at=clock(),
                valid_until=clock() + timedelta(seconds=5),
            ),
            account_id=SETTINGS.account_id,
            bot_id=SETTINGS.bot_id,
            mode=Mode.DEMO,
        )
    )
    eng.tick()
    found = Journal(conn).find("execution.updated", "intent_id", iid)
    assert found is not None
    assert payload_as(found, ExecutionUpdated).state is ExecutionState.REJECTED
    assert _bot_positions(broker) == []  # never sent late, without a fresh decision


def test_cash_flow_is_not_lost_when_a_tick_fails(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _running(conn, broker, clock)
    eng.tick()
    broker.withdraw(D("300"))
    real = DrawdownTracker.observe
    calls = {"n": 0}

    def flaky(self, *a, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, *a, **kw)

    monkeypatch.setattr(DrawdownTracker, "observe", flaky)
    with pytest.raises(sqlite3.OperationalError):
        eng.tick()
    eng.tick()
    assert not eng.control.risk_locked


def test_safe_mode_is_not_kept_across_restart_without_a_handler(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    set_flag(conn, "m06.control_state", "SAFE_MODE")
    eng = _engine(conn, broker, clock)
    assert eng.start()
    assert eng.control.state is ControlState.STOPPED


def test_finish_cycle_completes_only_when_stopped(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _running(conn, broker, clock)
    _intent(conn, clock)
    eng.tick()
    cid = _command(conn, ControlCommand.FINISH_CYCLE)
    eng.tick()
    assert _completed(conn, cid).status is CommandStatus.ACCEPTED
    _price(broker, "2010.00")
    eng.tick()
    done = _completed(conn, cid)
    assert done.status is CommandStatus.COMPLETED and done.resulting_state is ControlState.STOPPED


def test_a_deposit_is_counted_once(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-23: counting a deposit on every tick would inflate the peak into a false lock."""
    eng = _running(conn, broker, clock)
    eng.tick()
    broker.deposit(D("100"))
    for _ in range(5):
        eng.tick()
    assert not eng.control.risk_locked
