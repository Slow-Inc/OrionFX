"""Cycle book: Cycle Net, Profit Target and Basket Trailing (PRD-06 #15; PFT-01..05;
master plan §4.1, §4.2; CAT-R01-04).

- Cycle Net = realized net + floating net - estimated remaining close cost; no
  double counting (§4.1).
- Reaching the Profit Target latches CLOSING at once (Target Triggered); the final
  realized net decides Target Achieved separately (AT-02).
- Basket Trailing: once net reaches the activation level the floor is
  peak - distance and only ever rises; net <= floor latches CLOSING (AT-03).
  Proposed trailing parameters are clamped to protected bounds; with no AI input
  the caller passes the policy defaults [PROPOSED, OD-05].
- A cycle is CLOSED only via `mark_closed`, which the close workflow calls when the
  broker shows no bot exposure left.
"""

import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal

from app.core.contracts import CycleUpdated
from app.core.enums import CloseReason, CycleState
from app.core.ids import new_id
from app.core.states import IllegalTransition, next_cycle_state
from app.core.time import Clock, utc_now
from app.execution.broker import AccountInfo
from app.risk.policy import RiskPolicy
from app.storage.db import dec_or_none as _dec
from app.storage.db import text_or_none as _txt
from app.storage.db import transaction


def cycle_net(*, realized: Decimal, floating: Decimal, est_close_cost: Decimal) -> Decimal:
    return realized + floating - est_close_cost


@dataclass(frozen=True, slots=True)
class TrailingParams:
    activation_usd: Decimal
    distance_usd: Decimal

    def clamped(self, target: Decimal, policy: RiskPolicy) -> "TrailingParams":
        lo_act = target * policy.trailing_min_activation_fraction
        hi_act = target * policy.trailing_max_activation_fraction
        lo_dist = target * policy.trailing_min_distance_fraction
        hi_dist = target * policy.trailing_max_distance_fraction
        return TrailingParams(
            activation_usd=min(max(self.activation_usd, lo_act), hi_act),
            distance_usd=min(max(self.distance_usd, lo_dist), hi_dist),
        )


def default_trailing(target: Decimal, policy: RiskPolicy) -> TrailingParams:
    """Used when no validated AI proposal exists; never weakens an existing floor."""
    return TrailingParams(
        activation_usd=target * policy.trailing_default_activation_fraction,
        distance_usd=target * policy.trailing_default_distance_fraction,
    )


@dataclass(frozen=True, slots=True)
class CycleSnapshot:
    cycle_id: str
    state: CycleState
    target_usd: Decimal
    config_version: int
    last_net_usd: Decimal | None
    peak_net_usd: Decimal | None
    trailing_active: bool
    trailing_floor_usd: Decimal | None
    close_reason: CloseReason | None
    target_triggered: bool
    final_net_usd: Decimal | None

    @property
    def target_achieved(self) -> bool | None:
        return None if self.final_net_usd is None else self.final_net_usd >= self.target_usd

    @property
    def cycle_win(self) -> bool | None:
        return None if self.final_net_usd is None else self.final_net_usd > 0

    def as_event(self, unresolved: list[str] | None = None) -> CycleUpdated:
        return CycleUpdated(
            state=self.state,
            target_usd=self.target_usd,
            cycle_net_estimated_usd=self.last_net_usd,
            peak_net_usd=self.peak_net_usd,
            trailing_floor_usd=self.trailing_floor_usd,
            trailing_active=self.trailing_active,
            final_net_usd=self.final_net_usd,
            close_reason=self.close_reason,
            target_triggered=self.target_triggered,
            target_achieved=self.target_achieved,
            unresolved=unresolved or [],
        )


@dataclass(frozen=True, slots=True)
class CycleStats:
    cycle_win: bool | None
    target_achieved: bool | None


class CycleBook:
    def __init__(
        self, conn: sqlite3.Connection, *, account: AccountInfo, clock: Clock = utc_now
    ) -> None:
        self._conn = conn
        self._login = account.login
        self._server = account.server
        self._clock = clock

    # --- reads -------------------------------------------------------------------------

    def snapshot(self, cycle_id: str) -> CycleSnapshot:
        r = self._conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        if r is None:
            raise KeyError(cycle_id)
        return CycleSnapshot(
            cycle_id=r["cycle_id"],
            state=CycleState(r["state"]),
            target_usd=Decimal(r["target_usd"]),
            config_version=int(r["config_version"]),
            last_net_usd=_dec(r["last_net"]),
            peak_net_usd=_dec(r["peak_net"]),
            trailing_active=bool(r["trailing_active"]),
            trailing_floor_usd=_dec(r["trailing_floor"]),
            close_reason=None if r["close_reason"] is None else CloseReason(r["close_reason"]),
            target_triggered=bool(r["target_triggered"]),
            final_net_usd=_dec(r["final_net"]),
        )

    def open_cycle_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT cycle_id FROM cycles WHERE login = ? AND server = ? AND state != ?"
            " ORDER BY opened_at DESC LIMIT 1",
            (self._login, self._server, CycleState.CLOSED),
        ).fetchone()
        return None if row is None else str(row[0])

    def stats(self, cycle_id: str) -> CycleStats:
        s = self.snapshot(cycle_id)
        return CycleStats(s.cycle_win, s.target_achieved)

    # --- writes ------------------------------------------------------------------------

    def _save(self, s: CycleSnapshot) -> None:
        self._conn.execute(
            "UPDATE cycles SET state = ?, last_net = ?, peak_net = ?, trailing_active = ?,"
            " trailing_floor = ?, close_reason = ?, target_triggered = ?, final_net = ?,"
            " closed_at = COALESCE(closed_at, ?) WHERE cycle_id = ?",
            (
                s.state,
                _txt(s.last_net_usd),
                _txt(s.peak_net_usd),
                int(s.trailing_active),
                _txt(s.trailing_floor_usd),
                s.close_reason,
                int(s.target_triggered),
                _txt(s.final_net_usd),
                self._clock().isoformat() if s.state is CycleState.CLOSED else None,
                s.cycle_id,
            ),
        )

    def open_cycle(self, *, target_usd: Decimal, config_version: int) -> str:
        """IDLE -> ACTIVE. Refused while another cycle on this account is not CLOSED."""
        if target_usd <= 0:
            raise ValueError("Profit Target must be positive")
        with transaction(self._conn):
            if self.open_cycle_id() is not None:
                raise IllegalTransition("a cycle is still open on this account")
            cycle_id = new_id("cycle")
            next_cycle_state(CycleState.IDLE, CycleState.ACTIVE)
            self._conn.execute(
                "INSERT INTO cycles (cycle_id, login, server, state, target_usd, config_version,"
                " opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cycle_id,
                    self._login,
                    self._server,
                    CycleState.ACTIVE,
                    str(target_usd),
                    config_version,
                    self._clock().isoformat(),
                ),
            )
        return cycle_id

    def evaluate(
        self, cycle_id: str, *, net: Decimal, trailing: TrailingParams, policy: RiskPolicy
    ) -> CycleUpdated:
        """Apply one Cycle Net observation. Target first, then trailing (§4.2)."""
        with transaction(self._conn):
            s = self.snapshot(cycle_id)
            if s.state is not CycleState.ACTIVE:
                return s.as_event()  # CLOSING/CLOSED are latched
            params = trailing.clamped(s.target_usd, policy)
            peak = net if s.peak_net_usd is None else max(s.peak_net_usd, net)
            active = s.trailing_active or peak >= params.activation_usd
            floor = s.trailing_floor_usd
            if active:
                candidate = peak - params.distance_usd
                floor = candidate if floor is None else max(floor, candidate)
            s = replace(
                s,
                last_net_usd=net,
                peak_net_usd=peak,
                trailing_active=active,
                trailing_floor_usd=floor,
            )
            if net >= s.target_usd:
                s = replace(
                    s,
                    state=next_cycle_state(s.state, CycleState.CLOSING),
                    close_reason=CloseReason.TARGET_TRIGGERED,
                    target_triggered=True,
                )
            elif floor is not None and net <= floor:
                s = replace(
                    s,
                    state=next_cycle_state(s.state, CycleState.CLOSING),
                    close_reason=CloseReason.TRAILING_FLOOR,
                )
            self._save(s)
        return s.as_event()

    def begin_closing(self, cycle_id: str, reason: CloseReason) -> CycleUpdated:
        """Latch CLOSING for a non-profit reason (emergency stop, DD breach, finish, AI exit)."""
        with transaction(self._conn):
            s = self.snapshot(cycle_id)
            if s.state is CycleState.ACTIVE:
                s = replace(
                    s, state=next_cycle_state(s.state, CycleState.CLOSING), close_reason=reason
                )
                self._save(s)
        return s.as_event()

    def mark_closed(self, cycle_id: str, *, final_net: Decimal) -> CycleUpdated:
        with transaction(self._conn):
            s = self.snapshot(cycle_id)
            s = replace(
                s, state=next_cycle_state(s.state, CycleState.CLOSED), final_net_usd=final_net
            )
            self._save(s)
        return s.as_event()
