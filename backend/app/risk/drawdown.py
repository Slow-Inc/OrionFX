"""Account DD tracker (PRD-06 #14; RSK-01..03, RSK-07; master plan §4.3).

DD% = max(0, (peak - equity) / peak x 100) on the whole account equity, manual
trades included. Deposits/withdrawals move both peaks by their amount, so cash
flow is never read as trading P&L (AT-23) [PROPOSED rule, OD-04]. A breach of
the epoch ceiling (the owner's max DD) or the lifetime ceiling latches a lock
that survives restarts and never clears because equity recovered. Approved
recovery starts a new epoch; the lifetime peak never resets, and a lifetime
breach cannot be approved away.
"""

import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal

from app.core.contracts import RiskUpdated
from app.core.time import Clock, utc_now
from app.risk.policy import RiskPolicy
from app.storage.db import transaction

HUNDRED = Decimal(100)


def dd_percent(peak: Decimal, equity: Decimal) -> Decimal:
    if peak <= 0:
        return HUNDRED
    return min(HUNDRED, max(Decimal(0), (peak - equity) / peak * HUNDRED))


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
        self, conn: sqlite3.Connection, *, account_key: str, clock: Clock = utc_now
    ) -> None:
        self._conn = conn
        self._key = account_key
        self._clock = clock

    def state(self) -> DrawdownState:
        row = self._conn.execute(
            "SELECT epoch_id, epoch_peak, lifetime_peak, locked, lock_reason, lifetime_breached"
            " FROM risk_state WHERE account_key = ?",
            (self._key,),
        ).fetchone()
        if row is None:
            return DrawdownState()
        return DrawdownState(
            epoch_id=int(row[0]),
            epoch_peak=None if row[1] is None else Decimal(row[1]),
            lifetime_peak=None if row[2] is None else Decimal(row[2]),
            locked=bool(row[3]),
            lock_reason=row[4],
            lifetime_breached=bool(row[5]),
        )

    def _save(self, s: DrawdownState) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO risk_state (account_key, epoch_id, epoch_peak, lifetime_peak, locked,"
                " lock_reason, lifetime_breached, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(account_key) DO UPDATE SET epoch_id = excluded.epoch_id,"
                " epoch_peak = excluded.epoch_peak, lifetime_peak = excluded.lifetime_peak,"
                " locked = excluded.locked, lock_reason = excluded.lock_reason,"
                " lifetime_breached = excluded.lifetime_breached, updated_at = excluded.updated_at",
                (
                    self._key,
                    s.epoch_id,
                    None if s.epoch_peak is None else str(s.epoch_peak),
                    None if s.lifetime_peak is None else str(s.lifetime_peak),
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
        s = self.state()
        epoch_peak = equity if s.epoch_peak is None else s.epoch_peak + cash_flow
        lifetime_peak = equity if s.lifetime_peak is None else s.lifetime_peak + cash_flow
        epoch_peak, lifetime_peak = max(epoch_peak, equity), max(lifetime_peak, equity)

        epoch_dd = dd_percent(epoch_peak, equity)
        lifetime_dd = dd_percent(lifetime_peak, equity)
        lifetime_ceiling = policy.lifetime_ceiling(owner_max_dd)

        reasons: list[str] = []
        if epoch_dd >= owner_max_dd:
            reasons.append("EPOCH_DD_BREACH")
        if lifetime_dd >= lifetime_ceiling:
            reasons.append("LIFETIME_DD_BREACH")

        s = replace(
            s,
            epoch_peak=epoch_peak,
            lifetime_peak=lifetime_peak,
            locked=s.locked or bool(reasons),
            lock_reason=s.lock_reason or (reasons[0] if reasons else None),
            lifetime_breached=s.lifetime_breached or "LIFETIME_DD_BREACH" in reasons,
        )
        self._save(s)
        if s.locked and not reasons and s.lock_reason:
            reasons.append(s.lock_reason)  # still locked from an earlier breach
        return RiskUpdated(
            epoch_dd_percent=epoch_dd,
            lifetime_dd_percent=lifetime_dd,
            epoch_ceiling_percent=owner_max_dd,
            lifetime_ceiling_percent=lifetime_ceiling,
            risk_locked=s.locked,
            reason_codes=reasons,
            policy_version=policy.policy_version,
        )

    def approve_recovery(self, equity: Decimal) -> bool:
        """New epoch after an epoch breach (RSK-07). Refused without a lock or after a
        lifetime breach; the caller (M06 engine) checks the remaining gates."""
        s = self.state()
        if not s.locked or s.lifetime_breached:
            return False
        self._save(
            replace(s, epoch_id=s.epoch_id + 1, epoch_peak=equity, locked=False, lock_reason=None)
        )
        return True
