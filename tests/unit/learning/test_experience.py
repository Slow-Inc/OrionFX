"""M07 slice A: experience log and point-in-time outcome labels (#59; LRN-02, LRN-06;
AT-15). Every decision becomes an experience, WAIT included; a skipped signal is
labelled only by the market's move, never by an invented fill."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.decision import T0, _ctx

from app.core.contracts import CandleClosed, DecisionCreated
from app.core.enums import DecisionAction, Direction, Mode, Side, Timeframe
from app.learning.experience import (
    ExperienceConflict,
    ExperienceLog,
    LabelKind,
    label_due,
    regime_of,
    vector_of,
)
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate

D = Decimal
H1 = Timeframe.H1


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "learn.db")
    migrate(c)
    yield c
    c.close()


def _decision(action: DecisionAction = DecisionAction.WAIT, did: str = "dec-1") -> DecisionCreated:
    return DecisionCreated(
        decision_id=did, action=action, strategy_id="trend_following",
        side=Side.BUY if action is DecisionAction.ENTER else None, expected_value_usd=None,
        confidence=None, reason_codes=["EV_UNKNOWN"], evidence_refs=["snap"],
        policy_version="p1",
    )  # fmt: skip


def _record(log: ExperienceLog, **over: object) -> None:
    args: dict[str, object] = {
        "decision": _decision(), "snapshot": _ctx().snapshot, "mode": Mode.DEMO,
        "symbol": "XAUUSD", "cycle_id": "cyc_1", "direction": Direction.UP, "horizon": H1,
        "horizon_bars": 1,
    }  # fmt: skip
    args.update(over)
    log.record(**args)  # type: ignore[arg-type]


def _bar(open_time: datetime, close: str) -> CandleClosed:
    c = D(close)
    return CandleClosed(
        symbol="XAUUSD", timeframe=H1, open_time=open_time,
        close_time=open_time + timedelta(hours=1), open=c, high=c, low=c, close=c, volume=None,
        available_at=open_time + timedelta(hours=1, seconds=1),
    )  # fmt: skip


# --- recording ------------------------------------------------------------------------------------


def test_every_decision_including_wait_is_recorded_once(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log)
    _record(log)  # the same decision again: a no-op
    _record(log, decision=_decision(DecisionAction.ENTER, "dec-2"))
    rows = log.experiences()
    assert [(e.decision_id, e.action) for e in rows] == [
        ("dec-1", DecisionAction.WAIT),
        ("dec-2", DecisionAction.ENTER),
    ]
    assert rows[0].snapshot_ref == _ctx().snapshot.ref and rows[0].as_of == T0


def test_a_different_body_for_a_recorded_decision_is_refused(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log)
    with pytest.raises(ExperienceConflict):
        _record(log, mode=Mode.SHADOW)


def test_experiences_are_append_only(conn: sqlite3.Connection) -> None:
    _record(ExperienceLog(conn, clock=lambda: T0))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE learning_experiences SET action = 'ENTER'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM learning_experiences")


def test_vector_and_regime_come_from_the_snapshot() -> None:
    from app.decision.strategies import ADX_H1, RANGE_M15, RSI_M5

    snap = _ctx().snapshot.model_copy(
        update={"values": {RANGE_M15: D(1), RSI_M5: D(55), ADX_H1: None}}
    )
    v = vector_of(snap)
    assert v[RSI_M5] == D("0.55") and v[ADX_H1] is None  # missing stays missing, not zero
    assert regime_of(snap) == "RANGE"
    assert regime_of(_ctx().snapshot.model_copy(update={"values": {}})) == "UNKNOWN"


# --- labels ---------------------------------------------------------------------------------------


def _candles(conn: sqlite3.Connection, *closes: tuple[datetime, str]) -> CandleStore:
    store = CandleStore(conn, dataset_version="v1")
    for t, c in closes:
        store.save(_bar(t, c))
    return store


def test_a_skipped_signal_gets_a_market_move_label_only_after_its_horizon(
    conn: sqlite3.Connection,
) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log)  # WAIT at T0 (12:00), horizon 1 x H1
    candles = _candles(conn, (T0 - timedelta(hours=1), "2000"), (T0, "2004.50"))
    early = T0 + timedelta(minutes=30)
    assert label_due(conn, candles, now=early) == []  # the horizon bar has not closed yet
    (label,) = label_due(conn, candles, now=T0 + timedelta(hours=2))
    assert label.kind is LabelKind.MARKET_MOVE and label.value == D("4.50")
    assert label.direction is Direction.UP
    assert label.available_at == T0 + timedelta(hours=1, seconds=1)  # known when the bar was
    assert label_due(conn, candles, now=T0 + timedelta(hours=3)) == []  # labelled once


def test_a_skipped_signal_never_gets_a_pnl_label(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log)
    candles = _candles(conn, (T0 - timedelta(hours=1), "2000"), (T0, "1990"))
    (label,) = label_due(conn, candles, now=T0 + timedelta(hours=2))
    assert label.kind is not LabelKind.REALIZED and label.direction is Direction.DOWN


def _close_cycle(conn: sqlite3.Connection, final: str, closed_at: datetime) -> None:
    conn.execute(
        "INSERT INTO cycles (cycle_id, login, server, state, target_usd, config_version,"
        " final_net, opened_at, closed_at) VALUES ('cyc_1', 'l', 's', 'CLOSED', '10', 1, ?, ?, ?)",
        (final, T0.isoformat(), closed_at.isoformat()),
    )


def test_realized_label_is_known_only_from_the_cycle_close(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log, decision=_decision(DecisionAction.ENTER, "dec-2"))
    candles = _candles(conn)
    closed = T0 + timedelta(hours=3)
    assert label_due(conn, candles, now=closed) == []  # the cycle has not closed
    _close_cycle(conn, "12.40", closed)
    assert label_due(conn, candles, now=closed - timedelta(minutes=1)) == []  # not yet known
    (label,) = label_due(conn, candles, now=closed)
    assert label.kind is LabelKind.REALIZED and label.value == D("12.40")
    assert label.available_at == closed and label.source_refs == ["cycle:cyc_1"]


def test_labels_are_stored_append_only(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log)
    candles = _candles(conn, (T0 - timedelta(hours=1), "2000"), (T0, "2004.50"))
    label_due(conn, candles, now=T0 + timedelta(hours=2))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE learning_labels SET value = '0'")


def test_unlabelled_when_the_market_data_is_missing(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log)
    assert label_due(conn, _candles(conn), now=T0 + timedelta(hours=5)) == []


def test_a_multi_bar_horizon_waits_for_its_last_bar(conn: sqlite3.Connection) -> None:
    log = ExperienceLog(conn, clock=lambda: T0)
    _record(log, horizon_bars=2)  # ends at T0 + 2h
    candles = _candles(
        conn, (T0 - timedelta(hours=1), "2000"), (T0, "2003"), (T0 + timedelta(hours=1), "1995")
    )
    assert label_due(conn, candles, now=T0 + timedelta(hours=1, minutes=30)) == []
    (label,) = label_due(conn, candles, now=T0 + timedelta(hours=3))
    assert label.value == D(-5) and label.direction is Direction.DOWN
