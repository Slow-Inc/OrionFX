"""M06 slice B: Account DD tracker (PRD-06 #14; RSK-01..03, RSK-07; AT-06, AT-07, AT-23)."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.contracts import RiskUpdated
from app.execution.broker import AccountInfo, MarginMode
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


ACCOUNT = AccountInfo(
    login="demo-1",
    server="FakeBroker-Demo",
    currency="USD",
    balance=D(1000),
    equity=D(1000),
    margin=D(0),
    free_margin=D(1000),
    margin_mode=MarginMode.HEDGING,
    connected=True,
)


def _tracker(conn: sqlite3.Connection) -> DrawdownTracker:
    return DrawdownTracker(conn, account=ACCOUNT)


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
    assert t.approve_recovery(D("790"), actor="owner")
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
    assert not t.approve_recovery(D("690"), actor="owner")
    assert t.state().locked


def test_approve_without_lock_is_refused(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert not t.approve_recovery(D("1000"), actor="owner")


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


def test_lifetime_breach_keeps_being_reported(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    t.observe(D("790"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)  # epoch breach first
    t.observe(D("690"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)  # then lifetime
    upd = t.observe(D("700"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    assert "LIFETIME_DD_BREACH" in upd.reason_codes


def test_approve_recovery_is_audited(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    t.observe(D("1000"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    t.observe(D("790"), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)
    t.approve_recovery(D("790"), actor="owner")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log")]
    assert "risk.approve_recovery" in actions


def _observe(t: DrawdownTracker, equity: str, cash_flow: str = "0") -> RiskUpdated:
    return t.observe(D(equity), cash_flow=D(cash_flow), owner_max_dd=MAX_DD, policy=DEFAULT_POLICY)


def test_a_large_withdrawal_at_a_small_dd_keeps_the_dd(conn: sqlite3.Connection) -> None:
    """B2: peak 1000, equity 950 (5%), withdraw 900 -> equity 50. The additive rule
    moved the peak to 100 (50% DD, a permanent lifetime breach); scaling keeps 5%."""
    t = _tracker(conn)
    _observe(t, "1000")
    _observe(t, "950")
    upd = _observe(t, "50", "-900")
    nine = D("1e-9")  # the scaled peak 1000 x 50 / 950 is not exact in 28 digits
    assert upd.epoch_dd_percent.quantize(nine) == upd.lifetime_dd_percent.quantize(nine) == 5
    assert not upd.risk_locked


def test_a_deposit_does_not_hide_the_dd(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    _observe(t, "1000")
    _observe(t, "900")  # 10%
    upd = _observe(t, "1800", "900")  # doubled the account: the 10% is still there
    assert upd.epoch_dd_percent == D("10") and upd.lifetime_dd_percent == D("10")


def test_a_trading_loss_after_a_withdrawal_still_breaches(conn: sqlite3.Connection) -> None:
    t = _tracker(conn)
    _observe(t, "1000")
    _observe(t, "500", "-500")  # peak 500, no DD
    upd = _observe(t, "390")  # -22% from 500
    assert upd.risk_locked and "EPOCH_DD_BREACH" in upd.reason_codes


def test_a_full_withdrawal_still_fails_closed(conn: sqlite3.Connection) -> None:
    """Equity <= 0 handling is [TBD] in master plan section 4.3: it stays locked."""
    t = _tracker(conn)
    _observe(t, "1000")
    upd = _observe(t, "0", "-1000")
    assert upd.risk_locked and "LIFETIME_DD_BREACH" in upd.reason_codes
