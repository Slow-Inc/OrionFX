"""M06 slice C: cycle book, target, basket trailing, close workflow (PRD-06 #15;
PFT-01..05; AT-01..04; CAT-R01-04, CAT-R01-05)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import CloseReason, CycleState, Side
from app.core.states import IllegalTransition
from app.execution.broker import CloseRequest, OpenRequest, SendResult, SendStatus
from app.execution.executor import Executor
from app.execution.fake_broker import FakeBroker, Outcome, Partial
from app.profit.close import CloseWorkflow
from app.profit.cycle import CycleBook, TrailingParams, cycle_net
from app.risk.policy import DEFAULT_POLICY
from app.storage.db import connect, migrate

D = Decimal
T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "cyc.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker(balance=D("1000"), now=T0)
    b.set_quote("XAUUSD", bid=D("2000.00"), ask=D("2000.00"))
    return b


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _book(conn: sqlite3.Connection, broker: FakeBroker) -> CycleBook:
    return CycleBook(conn, account=broker.account())


NO_TRAIL = TrailingParams(activation_usd=D("1000"), distance_usd=D("1"))


# --- pure accounting ------------------------------------------------------------------


def test_cycle_net_counts_realized_floating_and_close_cost_once() -> None:
    assert cycle_net(realized=D("25"), floating=D("-15"), est_close_cost=D("0")) == D("10")
    assert cycle_net(realized=D("4"), floating=D("6"), est_close_cost=D("0.20")) == D("9.80")


# --- target ------------------------------------------------------------------------------


def test_target_reached_latches_closing(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    """AT-01: target $10, net $10 -> CLOSING immediately."""
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    upd = book.evaluate(cid, net=D("9.99"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    assert upd.state is CycleState.ACTIVE
    upd = book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    assert upd.state is CycleState.CLOSING and upd.target_triggered
    assert upd.close_reason is CloseReason.TARGET_TRIGGERED
    # latched: a dip back below target does not reopen it
    upd = book.evaluate(cid, net=D("8"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    assert upd.state is CycleState.CLOSING


def test_final_below_target_is_win_but_not_achieved(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    """AT-02: final realized $9.80 after a target-triggered close."""
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    upd = book.mark_closed(cid, final_net=D("9.80"))
    assert upd.state is CycleState.CLOSED
    assert upd.target_triggered is True and upd.target_achieved is False
    assert upd.final_net_usd == D("9.80")
    assert book.stats(cid).cycle_win is True


def test_cannot_open_a_new_cycle_while_one_is_open(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    book = _book(conn, broker)
    book.open_cycle(target_usd=D("10"), config_version=1)
    with pytest.raises(IllegalTransition):
        book.open_cycle(target_usd=D("10"), config_version=1)


def test_target_is_snapshotted_per_cycle(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=3)
    assert book.snapshot(cid).target_usd == D("10")
    assert book.snapshot(cid).config_version == 3


# --- basket trailing -------------------------------------------------------------------------


def test_floor_never_decreases_and_closes_at_floor(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    """AT-03: peak $9, distance $1.50 -> floor $7.50; floor holds; net at floor -> close."""
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    trail = TrailingParams(activation_usd=D("5"), distance_usd=D("1.50"))
    upd = book.evaluate(cid, net=D("4"), trailing=trail, policy=DEFAULT_POLICY)
    assert not upd.trailing_active
    book.evaluate(cid, net=D("6"), trailing=trail, policy=DEFAULT_POLICY)
    upd = book.evaluate(cid, net=D("9"), trailing=trail, policy=DEFAULT_POLICY)
    assert upd.trailing_active and upd.trailing_floor_usd == D("7.50")
    # a wider distance proposed later must not lower the floor
    wider = TrailingParams(activation_usd=D("5"), distance_usd=D("3"))
    upd = book.evaluate(cid, net=D("8"), trailing=wider, policy=DEFAULT_POLICY)
    assert upd.trailing_floor_usd == D("7.50") and upd.state is CycleState.ACTIVE
    upd = book.evaluate(cid, net=D("7.50"), trailing=wider, policy=DEFAULT_POLICY)
    assert upd.state is CycleState.CLOSING and upd.close_reason is CloseReason.TRAILING_FLOOR
    assert upd.target_triggered is False


def test_trailing_params_are_clamped_to_protected_bounds(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    reckless = TrailingParams(activation_usd=D("0.01"), distance_usd=D("100"))
    clamped = reckless.clamped(D("10"), DEFAULT_POLICY)
    assert clamped.activation_usd >= D("10") * DEFAULT_POLICY.trailing_min_activation_fraction
    assert clamped.distance_usd <= D("10") * DEFAULT_POLICY.trailing_max_distance_fraction
    book.evaluate(cid, net=D("1"), trailing=reckless, policy=DEFAULT_POLICY)
    assert not book.snapshot(cid).trailing_active


def test_trailing_state_survives_restart(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    trail = TrailingParams(activation_usd=D("5"), distance_usd=D("1.50"))
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    book.evaluate(cid, net=D("9"), trailing=trail, policy=DEFAULT_POLICY)
    again = _book(conn, broker)
    snap = again.snapshot(cid)
    assert (
        snap.trailing_active
        and snap.trailing_floor_usd == D("7.50")
        and snap.peak_net_usd == D("9")
    )


# --- close workflow ----------------------------------------------------------------------------


def _open_two(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock, cid: str, lot: str = "0.01"
) -> Executor:
    ex = Executor(conn, broker, clock=clock)
    for n in (1, 2):
        ex.open(
            OpenRequest(
                request_id=f"open-{n}", symbol="XAUUSD", side=Side.BUY, volume=D(lot), cycle_id=cid
            )
        )
    return ex


def test_close_workflow_closes_only_bot_positions_and_confirms(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    ex = _open_two(conn, broker, clock, cid)
    manual = broker.add_manual_position("XAUUSD", Side.SELL, D("0.03"))
    broker.set_quote("XAUUSD", bid=D("2005.00"), ask=D("2005.00"))
    book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    report = CloseWorkflow(conn, broker, ex, book).run(cid)
    assert report.closed and report.unresolved == []
    assert [p.position_id for p in broker.positions()] == [manual.position_id]
    snap = book.snapshot(cid)
    assert snap.state is CycleState.CLOSED and snap.final_net_usd == D("10.00")


def test_partial_and_lost_replies_never_duplicate_closes(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """AT-04: partial fill + timeout during closing -> reconcile, no duplicate order."""
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    ex = _open_two(conn, broker, clock, cid, lot="0.02")
    book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    broker.script(Partial(D("0.5")), Outcome.TIMEOUT_FILLED)
    wf = CloseWorkflow(conn, broker, ex, book)
    first = wf.run(cid)
    assert not first.closed and first.unresolved  # reported, not claimed
    assert book.snapshot(cid).state is CycleState.CLOSING
    for _ in range(3):
        report = wf.run(cid)
    assert report.closed
    closing_deals = [d for d in broker.deals_since(None) if d.side is Side.SELL]
    assert sum(d.volume for d in closing_deals) == D("0.04")  # exactly what was open


def test_market_closed_leaves_cycle_closing_with_unresolved(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    ex = _open_two(conn, broker, clock, cid)
    book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    broker.market_open = False
    report = CloseWorkflow(conn, broker, ex, book).run(cid)
    assert not report.closed and len(report.unresolved) == 2
    assert book.snapshot(cid).state is CycleState.CLOSING
    clock.now += timedelta(minutes=5)


def test_latched_closing_ignores_further_observations(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    book = _book(conn, broker)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    upd = book.evaluate(cid, net=D("12"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    assert upd.state is CycleState.CLOSING and upd.cycle_net_estimated_usd == D("10")


class LyingBroker(FakeBroker):
    """Reports a fill but leaves the position open: broker state, not replies, decides."""

    def send_close(self, req: CloseRequest) -> SendResult:  # type: ignore[override]
        return SendResult(SendStatus.ACCEPTED, req.position_id, (), req.volume, Decimal(2000))


def test_cycle_not_closed_while_broker_still_shows_positions(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    liar = LyingBroker(balance=D("1000"), now=T0)
    liar.set_quote("XAUUSD", bid=D("2000.00"), ask=D("2000.00"))
    book = _book(conn, liar)
    cid = book.open_cycle(target_usd=D("10"), config_version=1)
    ex = _open_two(conn, liar, clock, cid)
    book.evaluate(cid, net=D("10"), trailing=NO_TRAIL, policy=DEFAULT_POLICY)
    report = CloseWorkflow(conn, liar, ex, book).run(cid)
    assert not report.closed and report.unresolved
    assert book.snapshot(cid).state is CycleState.CLOSING
