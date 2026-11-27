"""M05 -> M06: decisions reach the risk engine only as journaled intents (#54; DEC-01,
AT-14, AT-41). The engine, not M05, decides and executes; M05 never touches the broker."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from tests.support.decision import COSTS, CYCLE, _ctx, _forecast, _news, _proposal
from tests.support.engine import (
    SETTINGS,
    Clock,
    _bot_positions,
    _command,
    _completed,
    _configure,
    _decision,
    _engine,
)

from app.core.enums import ControlCommand, ControlState, Direction, Mode, Side, Verdict
from app.decision.decide import decide
from app.decision.fusion import Decision, Evidence, to_events
from app.decision.manage import Held, manage
from app.engine.engine import Engine
from app.execution.fake_broker import FakeBroker
from app.profit.cycle import CycleBook
from app.storage.journal import Journal

D = Decimal


def _run(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock) -> Engine:
    _configure(conn, strategies_enabled=["trend_following"])
    eng = _engine(conn, broker, clock)
    assert eng.start()
    cid = _command(conn, ControlCommand.START)
    eng.tick()
    assert _completed(conn, cid).resulting_state is ControlState.RUNNING
    return eng


def _publish(conn: sqlite3.Connection, d: Decision) -> None:
    journal = Journal(conn)
    for e in to_events(d, account_id=SETTINGS.account_id, bot_id=SETTINGS.bot_id):
        journal.append(e)


def test_decisions_are_vetted_and_executed_by_m06(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    eng = _run(conn, broker, clock)
    cycle_id = CycleBook(conn, account=broker.account()).open_cycle_id()
    now, until = clock(), clock() + timedelta(hours=1)
    base = _ctx(now=now, mode=Mode.DEMO)
    snap = base.snapshot.model_copy(
        update={"as_of": now, "available_at": now, "valid_until": until}
    )
    ctx = replace(base, snapshot=snap)
    proposal = _proposal().model_copy(update={"valid_until": until})
    forecast = _forecast().model_copy(update={"generated_at": now, "valid_until": until})
    cycle = replace(CYCLE, cycle_id=cycle_id)

    d = decide(ctx, [proposal], Evidence(forecast=forecast), cycle=cycle, costs=COSTS,
               symbol="XAUUSD", policy_version="p1")  # fmt: skip
    assert d.intent is not None
    _publish(conn, d)
    eng.tick()
    assert _decision(conn, d.intent.intent_id).verdict is Verdict.ALLOW
    assert len(_bot_positions(broker)) == 1

    _publish(conn, d)  # a republish of the same decision is ignored: no second position
    eng.tick()
    assert len(_bot_positions(broker)) == 1

    # the evidence turns: M05 proposes CLOSE_ALL and M06 closes the bot's basket
    down = Direction.DOWN
    news = _news(down).model_copy(update={"available_at": now, "valid_until": until})
    turned = Evidence(forecast=forecast.model_copy(update={"direction": down}), news=[news])
    held = Held(Side.BUY, 1, D(0), _bot_positions(broker))
    exit_ = manage(ctx, [], turned, cycle=replace(cycle, open_positions=1), held=held,
                   max_positions=3, costs=COSTS, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    assert exit_.intent is not None
    _publish(conn, exit_)
    eng.tick()
    assert _decision(conn, exit_.intent.intent_id).verdict is Verdict.ALLOW
    assert _bot_positions(broker) == []
