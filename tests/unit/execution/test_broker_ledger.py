"""M06 slice A: fake broker, ownership, execution ledger (PRD-06 #13; AT-04, AT-06, AT-08)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import ExecutionState, Side
from app.execution.broker import BOT_MAGIC, CloseRequest, MarginMode, OpenRequest, SendStatus
from app.execution.executor import DispatchBlocked, Executor
from app.execution.fake_broker import FakeBroker, Outcome, Partial
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry
from app.storage.db import connect, migrate

T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)
D = Decimal


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "m06.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker(balance=D("1000"), now=T0)
    b.set_quote("XAUUSD", bid=D("2000.00"), ask=D("2000.20"))
    return b


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ex(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock) -> Executor:
    return Executor(conn, broker, clock=clock)


def _open(request_id: str, side: Side = Side.BUY, volume: str = "0.01") -> OpenRequest:
    return OpenRequest(
        request_id=request_id, symbol="XAUUSD", side=side, volume=D(volume), cycle_id="cyc-1"
    )


def _close(request_id: str, position_id: str, volume: str) -> CloseRequest:
    return CloseRequest(request_id=request_id, position_id=position_id, volume=D(volume))


# --- fake broker -------------------------------------------------------------------


def test_fake_broker_open_and_floating_pnl(broker: FakeBroker) -> None:
    r = broker.send_open(_open("req-1"))
    assert r.status is SendStatus.ACCEPTED and r.filled_volume == D("0.01")
    broker.set_quote("XAUUSD", bid=D("2010.00"), ask=D("2010.20"))
    (pos,) = broker.positions()
    # 0.01 lot * 100 oz * (2010.00 - 2000.20) = 9.80
    assert pos.profit == D("9.80")
    assert broker.account().equity == broker.balance + D("9.80")


def test_commission_charged_on_open_and_close() -> None:
    b = FakeBroker(balance=D("1000"), now=T0, commission_per_lot=D("7"))
    b.set_quote("XAUUSD", bid=D("2000.00"), ask=D("2000.00"))
    b.send_open(_open("o", volume="1"))
    (pos,) = b.positions()
    b.send_close(_close("c", pos.position_id, "1"))
    assert b.balance == D("986")  # 7 + 7, no price move


def test_fake_broker_close_realizes_deal(broker: FakeBroker) -> None:
    broker.send_open(_open("req-1"))
    (pos,) = broker.positions()
    broker.set_quote("XAUUSD", bid=D("2005.00"), ask=D("2005.20"))
    r = broker.send_close(_close("req-2", pos.position_id, "0.01"))
    assert r.status is SendStatus.ACCEPTED
    assert broker.positions() == []
    closing = broker.find_by_request("req-2")
    assert closing and closing[0].profit == D("4.80")


def test_fake_broker_scripted_outcomes(broker: FakeBroker) -> None:
    broker.script(Outcome.REJECT)
    assert broker.send_open(_open("req-1")).status is SendStatus.REJECTED
    broker.script(Partial(D("0.5")))
    r = broker.send_open(_open("req-2", volume="0.02"))
    assert r.status is SendStatus.ACCEPTED and r.filled_volume == D("0.01")
    broker.script(Outcome.TIMEOUT_FILLED)
    assert broker.send_open(_open("req-3")).status is SendStatus.UNKNOWN
    assert broker.find_by_request("req-3"), "the order did go through"
    broker.script(Outcome.TIMEOUT_LOST)
    assert broker.send_open(_open("req-4")).status is SendStatus.UNKNOWN
    assert broker.find_by_request("req-4") == []


def test_misspelled_outcome_is_rejected(broker: FakeBroker) -> None:
    with pytest.raises(TypeError):
        broker.script("timeout_fild")  # type: ignore[arg-type]


def test_fake_broker_market_closed_rejects(broker: FakeBroker) -> None:
    broker.market_open = False
    r = broker.send_open(_open("req-1"))
    assert r.status is SendStatus.REJECTED and "market closed" in (r.error or "")


def test_netting_merges_same_side_and_nets_opposite(broker: FakeBroker) -> None:
    broker.margin_mode = MarginMode.NETTING
    broker.send_open(_open("a"))
    broker.send_open(_open("b", volume="0.02"))
    (pos,) = broker.positions()
    assert pos.volume == D("0.03")
    broker.send_open(_open("c", side=Side.SELL, volume="0.05"))
    (flipped,) = broker.positions()
    assert flipped.side is Side.SELL and flipped.volume == D("0.02")


def test_cash_flow_is_a_balance_deal(broker: FakeBroker) -> None:
    broker.deposit(D("500"))
    deal = broker.deals_since(None)[-1]
    assert deal.is_balance and deal.profit == D("500")
    assert broker.account().balance == D("1500")


def test_deals_since_uses_deal_id_cursor(broker: FakeBroker) -> None:
    broker.send_open(_open("a"))
    broker.add_manual_position("XAUUSD", Side.BUY, D("0.01"))  # consumes an id, no deal
    broker.send_open(_open("b"))
    first, second = broker.deals_since(None)
    assert broker.deals_since(first.deal_id) == [second]


# --- ownership ---------------------------------------------------------------------


def test_manual_and_spoofed_positions_are_not_bot(
    conn: sqlite3.Connection, broker: FakeBroker, ex: Executor
) -> None:
    owners = OwnershipRegistry(conn)
    ex.open(_open("req-1"))
    broker.add_manual_position("XAUUSD", Side.SELL, D("0.05"))
    broker.add_manual_position("XAUUSD", Side.BUY, D("0.01"), magic=BOT_MAGIC)  # spoofed magic
    acct = broker.account()
    bot = owners.bot_positions(acct, broker.positions())
    assert [p.volume for p in bot] == [D("0.01")]
    assert owners.cycle_of(acct, bot[0].position_id) == "cyc-1"


def test_netting_with_manual_exposure_blocks_open(broker: FakeBroker, ex: Executor) -> None:
    broker.margin_mode = MarginMode.NETTING
    manual = broker.add_manual_position("XAUUSD", Side.BUY, D("0.10"))
    with pytest.raises(DispatchBlocked, match="ambiguous"):
        ex.open(_open("req-1", side=Side.SELL))
    (pos,) = broker.positions()
    assert pos.position_id == manual.position_id and pos.volume == D("0.10")


# --- ledger + executor ---------------------------------------------------------------


def test_request_is_persisted_before_dispatch(
    conn: sqlite3.Connection, broker: FakeBroker, ex: Executor
) -> None:
    seen: list[str | None] = []

    def spy(request_id: str) -> None:
        row = conn.execute(
            "SELECT state FROM exec_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        seen.append(row[0] if row else None)

    broker.on_send = spy
    ex.open(_open("req-1"))
    assert seen == ["PENDING"]


def test_duplicate_request_id_never_reaches_broker(broker: FakeBroker, ex: Executor) -> None:
    ex.open(_open("req-1"))
    with pytest.raises(DispatchBlocked, match="already"):
        ex.open(_open("req-1"))
    assert len(broker.positions()) == 1


def test_broker_exception_records_unknown(broker: FakeBroker, ex: Executor) -> None:
    def boom(request_id: str) -> None:
        raise ConnectionError("socket reset")

    broker.on_send = boom
    upd = ex.open(_open("req-1"))
    assert upd.state is ExecutionState.UNKNOWN and "socket reset" in (upd.error or "")


def test_unknown_outcome_blocks_until_reconciled(
    conn: sqlite3.Connection, broker: FakeBroker, ex: Executor
) -> None:
    broker.script(Outcome.TIMEOUT_FILLED)
    upd = ex.open(_open("req-1"))
    assert upd.state is ExecutionState.UNKNOWN
    with pytest.raises(DispatchBlocked, match="unknown"):
        ex.open(_open("req-2"))
    resolved = ex.reconcile()
    assert [(u.request_id, u.state) for u in resolved] == [("req-1", ExecutionState.FILLED)]
    acct = broker.account()
    assert len(OwnershipRegistry(conn).bot_positions(acct, broker.positions())) == 1
    ex.open(_open("req-2"))  # unblocked
    assert len(broker.positions()) == 2


def test_lost_timeout_stays_unknown_until_quiet_period(
    conn: sqlite3.Connection, broker: FakeBroker, ex: Executor, clock: Clock
) -> None:
    broker.script(Outcome.TIMEOUT_LOST)
    ex.open(_open("req-1"))
    assert ex.reconcile() == []  # too early: the order may still be in flight
    with pytest.raises(DispatchBlocked):
        ex.open(_open("req-2"))
    clock.now += timedelta(minutes=3)
    (u,) = ex.reconcile()
    assert u.state is ExecutionState.CANCELLED
    assert broker.positions() == []
    assert ExecutionLedger(conn).state("req-1") is ExecutionState.CANCELLED


def test_disconnected_broker_never_writes_off(
    broker: FakeBroker, ex: Executor, clock: Clock
) -> None:
    broker.script(Outcome.TIMEOUT_LOST)
    ex.open(_open("req-1"))
    broker.connected = False
    clock.now += timedelta(hours=1)
    assert ex.reconcile() == []


def test_partial_fill_recorded(broker: FakeBroker, ex: Executor) -> None:
    broker.script(Partial(D("0.5")))
    upd = ex.open(_open("req-1", volume="0.02"))
    assert upd.state is ExecutionState.PARTIALLY_FILLED and upd.filled_lot == D("0.01")


def test_close_requires_bot_ownership(broker: FakeBroker, ex: Executor) -> None:
    manual = broker.add_manual_position("XAUUSD", Side.BUY, D("0.05"))
    spoof = broker.add_manual_position("XAUUSD", Side.BUY, D("0.05"), magic=BOT_MAGIC)
    for pos in (manual, spoof):
        with pytest.raises(DispatchBlocked, match="not a bot position"):
            ex.close(_close(f"x-{pos.position_id}", pos.position_id, "0.05"))
    assert len(broker.positions()) == 2


def test_close_cannot_exceed_position(broker: FakeBroker, ex: Executor) -> None:
    ex.open(_open("req-1"))
    (pos,) = broker.positions()
    with pytest.raises(DispatchBlocked, match="exceeds"):
        ex.close(_close("c", pos.position_id, "0.02"))


def test_cancel_refuses_manual_pending_order(broker: FakeBroker, ex: Executor) -> None:
    manual = broker.add_pending("XAUUSD", Side.BUY, D("0.10"), D("1990"))
    spoof = broker.add_pending("XAUUSD", Side.BUY, D("0.10"), D("1990"), magic=BOT_MAGIC)
    for order in (manual, spoof):
        with pytest.raises(DispatchBlocked, match="not a bot order"):
            ex.cancel(order.order_id, f"x-{order.order_id}")
    assert len(broker.pending_orders()) == 2


def test_unknown_cancel_reconciles_from_pending_orders(
    conn: sqlite3.Connection, broker: FakeBroker, ex: Executor
) -> None:
    order = broker.add_pending("XAUUSD", Side.BUY, D("0.10"), D("1990"), magic=BOT_MAGIC)
    seed = ExecutionLedger(conn)
    seed.create(request_id="seed", kind="OPEN", requested_volume=D("0.10"))  # type: ignore[arg-type]
    seed.update("seed", ExecutionState.FILLED)
    OwnershipRegistry(conn).link_order(broker.account(), order.order_id, request_id="seed")
    broker.script(Outcome.TIMEOUT_LOST)
    ex.cancel(order.order_id, "cx-1")
    (u,) = ex.reconcile()
    assert u.state is ExecutionState.REJECTED  # the order is still there


def test_reconcile_reports_volume_weighted_price(broker: FakeBroker, ex: Executor) -> None:
    broker.script(Outcome.TIMEOUT_FILLED)
    ex.open(_open("req-1", volume="0.02"))
    (u,) = ex.reconcile()
    assert u.fill_price == D("2000.20")
