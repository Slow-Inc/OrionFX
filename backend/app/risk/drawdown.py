"""Account DD tracker (PRD-06 #14; RSK-01..03, RSK-07; master plan §4.3).

DD% = max(0, (peak - equity) / peak x 100) on the whole account equity, manual
trades included. Deposits/withdrawals scale both peaks by the equity ratio
after/before the cash flow, so DD% is unchanged by cash flow and never read as
trading P&L (AT-23) [PROPOSED rule, OD-04]. Moving the peaks by the amount instead
turned a large withdrawal at a small DD into a false lifetime breach, and let a
deposit hide DD (#121). With equity <= 0 before the cash flow there is no ratio,
so the peaks move by the amount as before. A full withdrawal scales the peaks to 0
and still locks (fail-closed; equity <= 0 handling is [TBD] in master plan §4.3).

A breach of the epoch ceiling (the owner's max DD) or the lifetime ceiling latches
a lock that survives restarts and never clears because equity recovered. Approved
recovery starts a new epoch; the lifetime peak never resets, and a lifetime
breach cannot be approved away.

Read-modify-write happens inside one write transaction, so two writers cannot
erase each other's breach. The row is rewritten only when the state changed.
"""

import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal

from app.core.contracts import RiskUpdated
from app.core.time import Clock, utc_now
from app.execution.broker import AccountInfo
from app.risk.policy import RiskPolicy
from app.storage.audit import audit
from app.storage.db import dec_or_none as _dec
from app.storage.db import text_or_none as _txt
from app.storage.db import transaction

HUNDRED = Decimal(100)
EPOCH_BREACH = "EPOCH_DD_BREACH"
LIFETIME_BREACH = "LIFETIME_DD_BREACH"


def dd_percent(peak: Decimal, equity: Decimal) -> Decimal:
    if peak <= 0:
        return HUNDRED
    return min(HUNDRED, max(Decimal(0), (peak - equity) / peak * HUNDRED))


def _adjust_peak(peak: Decimal | None, equity: Decimal, cash_flow: Decimal) -> Decimal:
    """The peak after this observation: scaled by the cash flow's equity ratio, then
    raised to equity. Without a positive pre-flow equity it moves by the amount."""
    if peak is None:
        return equity
    before = equity - cash_flow
    if cash_flow and before > 0:
        peak = peak * equity / before
    else:
        peak += cash_flow
    return max(peak, equity)


@dataclass(frozen=True, slots=True)
class DrawdownState:
    epoch_id: int = 1
    epoch_peak: Decimal | None = None
    lifetime_peak: Decimal | None = None
    locked: bool = False
    lock_reason: str | None = None
    lifetime_breached: bool = False


class DrawdownTracker:
    def __init__(
        self, conn: sqlite3.Connection, *, account: AccountInfo, clock: Clock = utc_now
    ) -> None:
        self._conn = conn
        self._login = account.login
        self._server = account.server
        self._clock = clock

    def state(self) -> DrawdownState:
        row = self._conn.execute(
            "SELECT epoch_id, epoch_peak, lifetime_peak, locked, lock_reason, lifetime_breached"
            " FROM risk_state WHERE login = ? AND server = ?",
            (self._login, self._server),
        ).fetchone()
        if row is None:
            return DrawdownState()
        return DrawdownState(
            epoch_id=int(row["epoch_id"]),
            epoch_peak=_dec(row["epoch_peak"]),
            lifetime_peak=_dec(row["lifetime_peak"]),
            locked=bool(row["locked"]),
            lock_reason=row["lock_reason"],
            lifetime_breached=bool(row["lifetime_breached"]),
        )

    def _save(self, s: DrawdownState) -> None:
        self._conn.execute(
            "INSERT INTO risk_state (login, server, epoch_id, epoch_peak, lifetime_peak, locked,"
            " lock_reason, lifetime_breached, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(login, server) DO UPDATE SET epoch_id = excluded.epoch_id,"
            " epoch_peak = excluded.epoch_peak, lifetime_peak = excluded.lifetime_peak,"
            " locked = excluded.locked, lock_reason = excluded.lock_reason,"
            " lifetime_breached = excluded.lifetime_breached, updated_at = excluded.updated_at",
            (
                self._login,
                self._server,
                s.epoch_id,
                _txt(s.epoch_peak),
                _txt(s.lifetime_peak),
                int(s.locked),
                s.lock_reason,
                int(s.lifetime_breached),
                self._clock().isoformat(),
            ),
        )

    def observe(
        self,
        equity: Decimal,
        *,
        owner_max_dd: Decimal,
        policy: RiskPolicy,
        cash_flow: Decimal = Decimal(0),
    ) -> RiskUpdated:
        with transaction(self._conn):
            old = self.state()
            epoch_peak = _adjust_peak(old.epoch_peak, equity, cash_flow)
            lifetime_peak = _adjust_peak(old.lifetime_peak, equity, cash_flow)

            epoch_dd = dd_percent(epoch_peak, equity)
            lifetime_dd = dd_percent(lifetime_peak, equity)
            lifetime_ceiling = policy.lifetime_ceiling(owner_max_dd)

            breaches = []
            if epoch_dd >= owner_max_dd:
                breaches.append(EPOCH_BREACH)
            if lifetime_dd >= lifetime_ceiling:
                breaches.append(LIFETIME_BREACH)

            new = replace(
                old,
                epoch_peak=epoch_peak,
                lifetime_peak=lifetime_peak,
                locked=old.locked or bool(breaches),
                lock_reason=old.lock_reason or (breaches[0] if breaches else None),
                lifetime_breached=old.lifetime_breached or LIFETIME_BREACH in breaches,
            )
            if new != old:
                self._save(new)

        reasons = list(breaches)
        if new.locked and new.lock_reason and new.lock_reason not in reasons:
            reasons.append(new.lock_reason)  # still locked from an earlier breach
        if new.lifetime_breached and LIFETIME_BREACH not in reasons:
            reasons.append(LIFETIME_BREACH)  # never approvable: always say so
        return RiskUpdated(
            epoch_dd_percent=epoch_dd,
            lifetime_dd_percent=lifetime_dd,
            epoch_ceiling_percent=owner_max_dd,
            lifetime_ceiling_percent=lifetime_ceiling,
            risk_locked=new.locked,
            reason_codes=reasons,
            policy_version=policy.policy_version,
        )

    def approve_recovery(self, equity: Decimal, *, actor: str) -> bool:
        """New epoch after an epoch breach (RSK-07). Refused without a lock or after a
        lifetime breach; the caller (M06 engine) checks the remaining gates."""
        with transaction(self._conn):
            s = self.state()
            if not s.locked or s.lifetime_breached:
                return False
            self._save(
                replace(
                    s, epoch_id=s.epoch_id + 1, epoch_peak=equity, locked=False, lock_reason=None
                )
            )
            audit(
                self._conn,
                actor=actor,
                action="risk.approve_recovery",
                detail=f"epoch {s.epoch_id + 1} from equity {equity}",
            )
        return True
