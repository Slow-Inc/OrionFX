"""M06 slice A: fake broker, ownership, execution ledger (PRD-06 #13; AT-04, AT-06, AT-08)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import ExecutionState, Side
from app.execution.broker import BOT_MAGIC, CloseRequest, MarginMode, OpenRequest, SendStatus
from app.execution.executor import DispatchBlocked, Executor
from app.execution.fake_broker import FakeBroker
from app.execution.ledger import ExecutionLedger
from app.execution.ownership import OwnershipRegistry
from app.storage.db import connect, migrate

T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)
D = Decimal


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


def _open(request_id: str, side: Side = Side.BUY, volume: str = "0.01") -> OpenRequest:
    return OpenRequest(
        request_id=request_id, symbol="XAUUSD", side=side, volume=D(volume), cycle_id="cyc-1"
    )


# --- fake broker -------------------------------------------------------------------


def test_fake_broker_open_and_floating_pnl(broker: FakeBroker) -> None:
    r = broker.send_open(_open("req-1"))
    assert r.status is SendStatus.ACCEPTED and r.filled_volume == D("0.01")
    broker.set_quote("XAUUSD", bid=D("2010.00"), ask=D("2010.20"))
    (pos,) = broker.positions()
    # 0.01 lot * 100 oz * (2010.00 - 2000.20) = 9.80
    assert pos.profit == D("9.80")
    assert broker.account().equity == D("1000") + D("9.80") - pos.commission


def test_fake_broker_close_realizes_deal(broker: FakeBroker) -> None:
    broker.send_open(_open("req-1"))
    (pos,) = broker.positions()
    broker.set_quote("XAUUSD", bid=D("2005.00"), ask=D("2005.20"))
    r = broker.send_close(
        CloseRequest(request_id="req-2", position_id=pos.position_id, volume=pos.volume)
    )
    assert r.status is SendStatus.ACCEPTED
    assert broker.positions() == []
    closing = [d for d in broker.deals_since(0) if d.request_id == "req-2"]
    assert closing and closing[0].profit == D("4.80")


def test_fake_broker_scripted_outcomes(broker: FakeBroker) -> None:
    broker.script("reject")
    assert broker.send_open(_open("req-1")).status is SendStatus.REJECTED
    broker.script("partial:0.5")
    r = broker.send_open(_open("req-2", volume="0.02"))
    assert r.status is SendStatus.ACCEPTED and r.filled_volume == D("0.01")
    broker.script("timeout_filled")
    assert broker.send_open(_open("req-3")).status is SendStatus.UNKNOWN
    assert broker.find_by_request("req-3"), "the order did go through"
    broker.script("timeout_lost")
    assert broker.send_open(_open("req-4")).status is SendStatus.UNKNOWN
    assert broker.find_by_request("req-4") == []


def test_fake_broker_market_closed_rejects(broker: FakeBroker) -> None:
    broker.market_open = False
    r = broker.send_open(_open("req-1"))
    assert r.status is SendStatus.REJECTED and "market closed" in (r.error or "")


def test_netting_merges_positions(broker: FakeBroker) -> None:
    broker.margin_mode = MarginMode.NETTING
    broker.add_manual_position("XAUUSD", Side.BUY, D("0.02"))
    broker.send_open(_open("req-1"))
    (pos,) = broker.positions()
    assert pos.volume == D("0.03")


def test_cash_flow_is_a_balance_deal(broker: FakeBroker) -> None:
    broker.deposit(D("500"))
    deal = broker.deals_since(0)[-1]
    assert deal.is_balance and deal.profit == D("500")
    assert broker.account().balance == D("1500")


# --- ownership ---------------------------------------------------------------------


def test_manual_and_unlinked_positions_are_not_bot(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    owners = OwnershipRegistry(conn)
    ex = Executor(conn, broker)
    ex.open(_open("req-1"))
    broker.add_manual_position("XAUUSD", Side.SELL, D("0.05"))
    broker.add_manual_position("XAUUSD", Side.BUY, D("0.01"), magic=BOT_MAGIC)  # spoofed magic
    bot = owners.bot_positions(broker.positions())
    assert [p.volume for p in bot] == [D("0.01")]
    assert bot[0].magic == BOT_MAGIC and owners.cycle_of(bot[0].position_id) == "cyc-1"


def test_netting_with_manual_exposure_is_ambiguous(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    owners = OwnershipRegistry(conn)
    assert not owners.ownership_ambiguous("XAUUSD", broker.positions(), broker.account())
    broker.margin_mode = MarginMode.NETTING
    broker.add_manual_position("XAUUSD", Side.BUY, D("0.02"))
    assert owners.ownership_ambiguous("XAUUSD", broker.positions(), broker.account())


# --- ledger + executor ---------------------------------------------------------------


def test_request_is_persisted_before_dispatch(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    seen: list[str | None] = []

    def spy(request_id: str) -> None:
        row = conn.execute(
            "SELECT state FROM exec_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        seen.append(row[0] if row else None)

    broker.on_send = spy
    Executor(conn, broker).open(_open("req-1"))
    assert seen == ["PENDING"]


def test_duplicate_request_id_never_reaches_broker(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    ex = Executor(conn, broker)
    ex.open(_open("req-1"))
    with pytest.raises(DispatchBlocked, match="already"):
        ex.open(_open("req-1"))
    assert len(broker.positions()) == 1


def test_unknown_outcome_blocks_until_reconciled(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    ex = Executor(conn, broker)
    broker.script("timeout_filled")
    upd = ex.open(_open("req-1"))
    assert upd.state is ExecutionState.UNKNOWN
    with pytest.raises(DispatchBlocked, match="unknown"):
        ex.open(_open("req-2"))
    resolved = ex.reconcile()
    assert [(u.request_id, u.state) for u in resolved] == [("req-1", ExecutionState.FILLED)]
    assert len(OwnershipRegistry(conn).bot_positions(broker.positions())) == 1
    ex.open(_open("req-2"))  # unblocked
    assert len(broker.positions()) == 2


def test_lost_timeout_reconciles_to_cancelled_without_retry(
    conn: sqlite3.Connection, broker: FakeBroker
) -> None:
    ex = Executor(conn, broker)
    broker.script("timeout_lost")
    ex.open(_open("req-1"))
    (u,) = ex.reconcile()
    assert u.state is ExecutionState.CANCELLED
    assert broker.positions() == []
    assert ExecutionLedger(conn).state("req-1") is ExecutionState.CANCELLED


def test_partial_fill_recorded(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    broker.script("partial:0.5")
    upd = Executor(conn, broker).open(_open("req-1", volume="0.02"))
    assert upd.state is ExecutionState.PARTIALLY_FILLED and upd.filled_lot == D("0.01")


def test_close_requires_bot_ownership(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    ex = Executor(conn, broker)
    manual = broker.add_manual_position("XAUUSD", Side.BUY, D("0.05"))
    with pytest.raises(DispatchBlocked, match="not a bot position"):
        ex.close(CloseRequest(request_id="req-x", position_id=manual.position_id, volume=D("0.05")))
    assert len(broker.positions()) == 1


def test_close_refuses_spoofed_magic(conn: sqlite3.Connection, broker: FakeBroker) -> None:
    spoof = broker.add_manual_position("XAUUSD", Side.BUY, D("0.05"), magic=BOT_MAGIC)
    with pytest.raises(DispatchBlocked, match="not a bot position"):
        Executor(conn, broker).close(
            CloseRequest(request_id="req-x", position_id=spoof.position_id, volume=D("0.05"))
        )
