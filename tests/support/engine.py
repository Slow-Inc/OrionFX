"""Shared helpers for the engine integration tests: the broker-driven clock, config,
commands, intents and journal lookups. A support module, not a test module."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import TradingConfig, UserConfig
from app.core.contracts import (
    ControlCompleted,
    ControlRequested,
    CycleUpdated,
    RiskDecision,
    TradeIntent,
)
from app.core.enums import (
    ControlCommand,
    ControlState,
    CycleState,
    IntentAction,
    Mode,
    Side,
)
from app.core.events import make_event
from app.engine.engine import Engine, EngineSettings
from app.execution.broker import BOT_MAGIC
from app.execution.fake_broker import FakeBroker
from app.storage.config_store import ConfigStore
from app.storage.journal import Journal, payload_as

T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)


class Clock:
    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker

    def __call__(self) -> datetime:
        return self.broker.now

    def advance(self, seconds: float) -> None:
        self.broker.now += timedelta(seconds=seconds)


D = Decimal


def _configure(conn: sqlite3.Connection, **trading: object) -> None:
    base: dict[str, object] = {
        "initial_lot": D("0.01"),
        "profit_target_usd": D("10"),
        "max_drawdown_percent": D("20"),
        "strategies_enabled": ["trend"],
    }
    base.update(trading)
    store = ConfigStore(conn)
    version, _ = store.current()
    store.update(
        UserConfig(trading=TradingConfig.model_validate(base)), expected_version=version, actor="t"
    )


def _engine(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock, holder: str = "engine-a"
) -> Engine:
    return Engine(conn, broker, settings=EngineSettings(holder=holder), clock=clock)


SETTINGS = EngineSettings()


def _command(conn: sqlite3.Connection, command: ControlCommand) -> str:
    cid = f"cmd-{command}-{Journal(conn).latest_seq()}"
    Journal(conn).append(
        make_event(
            "control.requested",
            source="M08",
            payload=ControlRequested(command_id=cid, command=command, actor="owner"),
            account_id=SETTINGS.account_id,
            bot_id=SETTINGS.bot_id,
            mode=Mode.DEMO,
        )
    )
    return cid


_n = 0


def _intent(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    action: IntentAction = IntentAction.OPEN,
    lot: str | None = "0.01",
    side: Side | None = Side.BUY,
    strategy: str = "trend",
    symbol: str = "XAUUSD",
    mode: Mode = Mode.DEMO,
    refs: list[str] | None = None,
) -> str:
    global _n
    _n += 1
    iid = f"intent-{_n}"
    Journal(conn).append(
        make_event(
            "trade.intent",
            source="M05",
            payload=TradeIntent(
                intent_id=iid,
                decision_id=f"dec-{_n}",
                action=action,
                side=side,
                symbol=symbol,
                requested_lot=None if lot is None else D(lot),
                strategy_id=strategy,
                cycle_id=None,
                mode=mode,
                expected_value_usd=None,
                prediction_ids=[],
                position_refs=refs or [],
                valid_until=clock() + timedelta(seconds=30),
            ),
            account_id=SETTINGS.account_id,
            bot_id=SETTINGS.bot_id,
            mode=mode,
        )
    )
    return iid


def _completed(conn: sqlite3.Connection, command_id: str) -> ControlCompleted:
    found = Journal(conn).find("control.completed", "command_id", command_id)
    assert found is not None, f"no control.completed for {command_id}"
    return payload_as(found, ControlCompleted)


def _decision(conn: sqlite3.Connection, intent_id: str) -> RiskDecision:
    found = Journal(conn).find("risk.decision", "intent_id", intent_id)
    assert found is not None, f"no risk.decision for {intent_id}"
    return payload_as(found, RiskDecision)


def _last_cycle(conn: sqlite3.Connection) -> CycleUpdated:
    (ev,) = Journal(conn).recent(["cycle.updated"], limit=1)
    return payload_as(ev, CycleUpdated)


def _closed_cycle(conn: sqlite3.Connection) -> CycleUpdated:
    """The newest cycle.updated that reports CLOSED."""
    for ev in Journal(conn).recent(["cycle.updated"], limit=20):
        upd = payload_as(ev, CycleUpdated)
        if upd.state is CycleState.CLOSED:
            return upd
    raise AssertionError("no CLOSED cycle.updated")


def _running(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock) -> Engine:
    _configure(conn)
    eng = _engine(conn, broker, clock)
    assert eng.start()
    cid = _command(conn, ControlCommand.START)
    eng.tick()
    assert _completed(conn, cid).resulting_state is ControlState.RUNNING
    return eng


def _bot_positions(broker: FakeBroker) -> list[str]:
    return [p.position_id for p in broker.positions() if p.magic == BOT_MAGIC]


def _price(broker: FakeBroker, price: str) -> None:
    broker.set_quote("XAUUSD", bid=D(price), ask=D(price))
