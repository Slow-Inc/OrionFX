"""M07 slice A: experience log and point-in-time outcome labels (#59; LRN-02, LRN-06;
AT-15). Every decision becomes an experience, WAIT included; a P&L label needs a real
fill, anything else is labelled only by the market's move over exactly its horizon."""

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
    RANGE_M15,
    TREND_M15,
    ExperienceConflict,
    ExperienceLog,
    LabelKind,
    regime_of,
    vector_of,
)
from app.market_data.features import feature_key
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


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    return CandleStore(conn, dataset_version="v1")


@pytest.fixture
def log(conn: sqlite3.Connection, candles: CandleStore) -> ExperienceLog:
    return ExperienceLog(conn, candles, clock=lambda: T0)


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
        "symbol": "XAUUSD", "cycle_id": "cyc_1", "intent_id": None, "direction": Direction.UP,
        "horizon": H1, "horizon_bars": 1,
    }  # fmt: skip
    args.update(over)
    log.record(**args)  # type: ignore[arg-type]


def _enter(log: ExperienceLog) -> None:
    _record(log, decision=_decision(DecisionAction.ENTER, "dec-2"), intent_id="int-2")


def _bars(candles: CandleStore, *closes: tuple[datetime, str]) -> None:
    for t, close in closes:
        c = D(close)
        candles.save(CandleClosed(
            symbol="XAUUSD", timeframe=H1, open_time=t, close_time=t + timedelta(hours=1),
            open=c, high=c, low=c, close=c, volume=None,
            available_at=t + timedelta(hours=1, seconds=1),
        ))  # fmt: skip


def _fill(conn: sqlite3.Connection, intent: str = "int-2", volume: str = "0.01") -> None:
    conn.execute(
        "INSERT INTO exec_requests (request_id, kind, intent_id, cycle_id, requested_volume,"
        " state, filled_volume, created_at, updated_at)"
        " VALUES ('req-1', 'OPEN', ?, 'cyc_1', '0.01', 'FILLED', ?, 't', 't')",
        (intent, volume),
    )


def _close_cycle(conn: sqlite3.Connection, final: str, closed_at: datetime) -> None:
    conn.execute(
        "INSERT INTO cycles (cycle_id, login, server, state, target_usd, config_version,"
        " final_net, opened_at, closed_at) VALUES ('cyc_1', 'l', 's', 'CLOSED', '10', 1, ?, ?, ?)",
        (final, T0.isoformat(), closed_at.isoformat()),
    )


# --- recording ---


def test_every_decision_including_wait_is_recorded_once(log: ExperienceLog) -> None:
    _record(log)
    _record(log)  # the same decision again: a no-op
    _enter(log)
    rows = log.experiences()
    assert [(e.decision_id, e.action) for e in rows] == [
        ("dec-1", DecisionAction.WAIT),
        ("dec-2", DecisionAction.ENTER),
    ]
    assert rows[0].snapshot_ref == _ctx().snapshot.ref and rows[0].as_of == T0


def test_a_different_body_for_a_recorded_decision_is_refused(log: ExperienceLog) -> None:
    _record(log)
    with pytest.raises(ExperienceConflict):
        _record(log, mode=Mode.SHADOW)


def test_experiences_are_append_only(conn: sqlite3.Connection, log: ExperienceLog) -> None:
    _record(log)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE learning_experiences SET action = 'ENTER'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM learning_experiences")


def test_vector_and_regime_come_from_the_snapshot() -> None:
    rsi = feature_key(Timeframe.M5, "CAT-T01-04", "rsi")
    adx = feature_key(H1, "CAT-T01-07", "adx")
    snap = _ctx().snapshot.model_copy(
        update={"values": {RANGE_M15: D(1), TREND_M15: D(0), rsi: D(55), adx: None}}
    )
    v = vector_of(snap)
    assert v[rsi] == D("0.55") and v[adx] is None  # missing stays missing, not zero
    assert regime_of(snap) == "RANGE"
    neither = {RANGE_M15: D(0), TREND_M15: D(0)}  # ADX between the two thresholds
    assert regime_of(snap.model_copy(update={"values": neither})) == "NEUTRAL"
    trending = {TREND_M15: D(1), RANGE_M15: D(0)}
    assert regime_of(snap.model_copy(update={"values": trending})) == "TREND"
    assert regime_of(snap.model_copy(update={"values": {}})) == "UNKNOWN"


# --- MARKET_MOVE labels ---


def test_a_skipped_signal_gets_a_market_move_label_only_after_its_horizon(
    log: ExperienceLog, candles: CandleStore
) -> None:
    _record(log)  # WAIT at T0 (12:00), horizon 1 x H1
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "2004.50"))
    assert log.label_due(now=T0 + timedelta(minutes=30)) == []  # horizon not over
    (label,) = log.label_due(now=T0 + timedelta(hours=2))
    assert label.kind is LabelKind.MARKET_MOVE and label.value == D("4.50")
    assert label.direction is Direction.UP
    assert label.available_at == T0 + timedelta(hours=1, seconds=1)  # known when the bar was
    assert label.source_refs[0].startswith("candle:v1:XAUUSD:H1:")
    assert log.label_due(now=T0 + timedelta(hours=3)) == []  # labelled once


def test_labels_are_read_point_in_time(log: ExperienceLog, candles: CandleStore) -> None:
    _record(log)
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "1990"))
    log.label_due(now=T0 + timedelta(hours=2))
    assert log.labels(as_of=T0 + timedelta(hours=1)) == {}  # not known yet at that time
    known = log.labels(as_of=T0 + timedelta(hours=2))["dec-1"]
    assert known.direction is Direction.DOWN and known.kind is LabelKind.MARKET_MOVE


def test_a_multi_bar_horizon_waits_for_its_last_bar(
    log: ExperienceLog, candles: CandleStore
) -> None:
    _record(log, horizon_bars=2)  # ends at T0 + 2h
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "2003"),
          (T0 + timedelta(hours=1), "1995"))  # fmt: skip
    assert log.label_due(now=T0 + timedelta(hours=1, minutes=30)) == []
    (label,) = log.label_due(now=T0 + timedelta(hours=3))
    assert label.value == D(-5) and label.direction is Direction.DOWN


def test_a_gap_means_no_label_not_a_stretched_horizon(
    log: ExperienceLog, candles: CandleStore
) -> None:
    _record(log)  # needs the bar closing at T0 + 1h
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0 + timedelta(hours=50), "2100"))
    assert log.label_due(now=T0 + timedelta(hours=60)) == []
    _record(log, decision=_decision(did="dec-3"), snapshot=_ctx().snapshot.model_copy(
        update={"as_of": T0 + timedelta(hours=10), "available_at": T0 + timedelta(hours=10),
                "valid_until": T0 + timedelta(hours=11)}))  # fmt: skip
    assert log.label_due(now=T0 + timedelta(hours=60)) == []  # its entry bar is missing


def test_a_filled_entry_is_labelled_from_its_cycle_close(
    conn: sqlite3.Connection, log: ExperienceLog
) -> None:
    _enter(log)
    _fill(conn)
    closed = T0 + timedelta(hours=3)
    assert log.label_due(now=closed) == []  # the cycle has not closed
    _close_cycle(conn, "12.40", closed)
    assert log.label_due(now=closed - timedelta(minutes=1)) == []  # not yet known
    (label,) = log.label_due(now=closed)
    assert label.kind is LabelKind.REALIZED and label.value == D("12.40")
    assert label.available_at == closed and "cycle:cyc_1" in label.source_refs


def test_a_refused_or_unfilled_entry_never_gets_a_pnl_label(
    conn: sqlite3.Connection, log: ExperienceLog, candles: CandleStore
) -> None:
    _enter(log)  # M06 refused it: no execution for int-2
    _close_cycle(conn, "12.40", T0 + timedelta(hours=3))
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "2002"))
    (label,) = log.label_due(now=T0 + timedelta(hours=4))
    assert label.kind is LabelKind.MARKET_MOVE and label.value == D(2)


def test_a_zero_volume_execution_is_not_a_fill(
    conn: sqlite3.Connection, log: ExperienceLog, candles: CandleStore
) -> None:
    _enter(log)
    _fill(conn, volume="0")
    _close_cycle(conn, "12.40", T0 + timedelta(hours=3))
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "2002"))
    (label,) = log.label_due(now=T0 + timedelta(hours=4))
    assert label.kind is LabelKind.MARKET_MOVE


def test_labels_are_stored_append_only(
    conn: sqlite3.Connection, log: ExperienceLog, candles: CandleStore
) -> None:
    _record(log)
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "2004.50"))
    log.label_due(now=T0 + timedelta(hours=2))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE learning_labels SET value = '0'")


def test_a_closed_cycles_final_net_is_frozen(conn: sqlite3.Connection) -> None:
    _close_cycle(conn, "12.40", T0)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE cycles SET final_net = '11.20' WHERE cycle_id = 'cyc_1'")
    conn.execute("UPDATE cycles SET final_net = '12.40' WHERE cycle_id = 'cyc_1'")  # same: ok


def test_a_run_that_labelled_first_is_not_repeated(
    conn: sqlite3.Connection, log: ExperienceLog, candles: CandleStore
) -> None:
    _record(log)
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "2004.50"))
    other = ExperienceLog(conn, candles, clock=lambda: T0)
    assert len(other.label_due(now=T0 + timedelta(hours=2))) == 1
    assert log.label_due(now=T0 + timedelta(hours=2)) == []
