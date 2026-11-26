"""Control and cycle state machines, and the safety priority order (§4.5, §9.1).

The risk lock is a separate persisted dimension: a control transition never
clears it. Only M06's approved-recovery path may (see `ControlStatus.unlock`).
"""

from dataclasses import dataclass, replace
from enum import IntEnum

from app.core.enums import ControlState, CycleState

_C = ControlState

CONTROL_TRANSITIONS: dict[ControlState, frozenset[ControlState]] = {
    _C.STOPPED: frozenset({_C.STARTING}),
    _C.STARTING: frozenset({_C.RECONCILING, _C.CLOSING, _C.RISK_LOCKED, _C.SAFE_MODE}),
    _C.RECONCILING: frozenset({_C.RUNNING, _C.STOPPED, _C.CLOSING, _C.SAFE_MODE, _C.RISK_LOCKED}),
    _C.RUNNING: frozenset({_C.PAUSED, _C.FINISHING, _C.CLOSING, _C.RISK_LOCKED, _C.SAFE_MODE}),
    _C.PAUSED: frozenset({_C.RUNNING, _C.FINISHING, _C.CLOSING, _C.RISK_LOCKED, _C.SAFE_MODE}),
    _C.FINISHING: frozenset({_C.STOPPED, _C.CLOSING, _C.RISK_LOCKED, _C.SAFE_MODE}),
    _C.CLOSING: frozenset({_C.STOPPED, _C.RISK_LOCKED, _C.SAFE_MODE}),
    _C.RISK_LOCKED: frozenset({_C.CLOSING, _C.SAFE_MODE, _C.RECONCILING}),
    _C.SAFE_MODE: frozenset({_C.RECONCILING, _C.CLOSING, _C.RISK_LOCKED}),
}

CYCLE_TRANSITIONS: dict[CycleState, frozenset[CycleState]] = {
    CycleState.IDLE: frozenset({CycleState.ACTIVE}),
    CycleState.ACTIVE: frozenset({CycleState.CLOSING}),
    CycleState.CLOSING: frozenset({CycleState.CLOSED}),
    CycleState.CLOSED: frozenset(),
}


class IllegalTransition(ValueError):
    pass


def _check[S: (ControlState, CycleState)](
    table: dict[S, frozenset[S]], current: S, target: S, kind: str
) -> S:
    if target not in table[current]:
        raise IllegalTransition(f"{kind} {current} -> {target} is not allowed")
    return target


class SafetyPriority(IntEnum):
    """Higher wins when several events compete (§4.5)."""

    ENTRY_OR_RECOVERY = 1
    RESTRICTING_CONTROL = 2
    PROFIT_EXIT = 3
    CLOSE_IN_PROGRESS = 4
    EMERGENCY_OR_RISK = 5


@dataclass(frozen=True, slots=True)
class ControlStatus:
    state: ControlState
    risk_locked: bool = False

    def to(self, target: ControlState) -> "ControlStatus":
        state = _check(CONTROL_TRANSITIONS, self.state, target, "control")
        if state is ControlState.RUNNING and self.risk_locked:
            raise IllegalTransition("risk lock is set: RUNNING needs approved recovery (unlock)")
        # Entering RISK_LOCKED always sets the persisted lock (§9.1).
        return replace(self, state=state, risk_locked=self.risk_locked or state is _C.RISK_LOCKED)

    def lock(self) -> "ControlStatus":
        return replace(self, risk_locked=True)

    def unlock(self) -> "ControlStatus":
        """Only for M06's approved-recovery path; never called by a transition."""
        return replace(self, risk_locked=False)


def allows_new_exposure(status: ControlStatus) -> bool:
    """Fail closed: new exposure only while RUNNING with no risk lock (§9.3)."""
    return status.state is ControlState.RUNNING and not status.risk_locked


def next_cycle_state(current: CycleState, target: CycleState) -> CycleState:
    return _check(CYCLE_TRANSITIONS, current, target, "cycle")
