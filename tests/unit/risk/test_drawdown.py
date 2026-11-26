"""M06 slice B: Account DD tracker (PRD-06 #14; RSK-01..03, RSK-07; AT-06, AT-07, AT-23)."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.risk.drawdown import DrawdownTracker, dd_percent
from app.risk.policy import DEFAULT_POLICY
from app.storage.db import connect, migrate

D = Decimal
MAX_DD = D("20")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "risk.db")
    migrate(c)
    yield c
    c.close()


def _tracker(conn: sqlite3.Connection) -> DrawdownTracker:
    return DrawdownTracker(conn, account_key="demo-1@FakeBroker-Demo")


def test_master_plan_example_peak_120_equity_105_is_12_5_percent() -> None:
    assert dd_percent(D("120"), D("105")) == D("12.5")


def test_dd_is_zero_at_or_above_peak_and_100_when_equity_gone() -> None:
    assert dd_percent(D("100"), D("130")) == 0
    assert dd_percent(D("100"), D("-5")) == 100
    assert dd_percent(D("0"), D("0")) == 100


def test_peaks_rise_with_equity(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    upd = t.observe(D("1100"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.epoch_dd_percent == 0
    upd = t.observe(D("990"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.epoch_dd_percent == D("10")
    assert not upd.risk_locked


def test_breach_by_manual_loss_latches_lock(conn: sqlite3.Connection) -> None:
    """AT-06: equity is the whole account, so a manual trade's loss counts too."""
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    upd = t.observe(D("800"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.risk_locked and "EPOCH_DD_BREACH" in upd.reason_codes
    # equity recovering never unlocks by itself
    upd = t.observe(D("1200"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.risk_locked


def test_lock_and_lifetime_peak_survive_restart(conn: sqlite3.Connection) -> None:
    """AT-07: a new tracker instance (process restart) reads the persisted state."""
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    t.observe(D("790"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    again = _tracker(conn)
    state = again.state()
    assert state.locked and state.lifetime_peak == D("1000")


def test_approve_recovery_starts_new_epoch_but_keeps_lifetime(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    t.observe(D("790"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert t.approve_recovery(D("790"))
    s = t.state()
    assert not s.locked and s.epoch_id == 2 and s.epoch_peak == D("790")
    assert s.lifetime_peak == D("1000")
    upd = t.observe(D("780"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.lifetime_dd_percent == D("22")  # still measured from the lifetime peak


def test_lifetime_breach_cannot_be_approved_away(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    # lifetime ceiling = min(20 * 1.5, 50) = 30%
    upd = t.observe(D("690"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert "LIFETIME_DD_BREACH" in upd.reason_codes
    assert not t.approve_recovery(D("690"))
    assert t.state().locked


def test_approve_without_lock_is_refused(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert not t.approve_recovery(D("1000"))


def test_deposit_and_withdrawal_are_not_trading_pnl(conn: sqlite3.Connection) -> None:
    """AT-23: cash flow moves the peak by the same amount."""
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    upd = t.observe(D("500"), cash_flow=D("-500"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.epoch_dd_percent == 0 and not upd.risk_locked
    upd = t.observe(D("1500"), cash_flow=D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.epoch_dd_percent == 0
    assert t.state().epoch_peak == D("1500")


def test_policy_version_reported(conn: sqlite3.Connection) -> None:
    upd = _tracker(conn).observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert upd.policy_version == DEFAULT_POLICY.policy_version
    assert "uncertified" in upd.policy_version
