"""Control/cycle state machines and safety priority (PRD-X01 X01-FR-005..007)."""

import pytest

from app.core.enums import ControlState, CycleState
from app.core.states import (
    CONTROL_TRANSITIONS,
    ControlStatus,
    IllegalTransition,
    SafetyPriority,
    allows_new_exposure,
    next_cycle_state,
)


def test_every_control_state_has_a_transition_row() -> None:
    assert set(CONTROL_TRANSITIONS) == set(ControlState)


def test_legal_control_path() -> None:
    s = ControlStatus(ControlState.STOPPED)
    for target in (
        ControlState.STARTING,
        ControlState.RECONCILING,
        ControlState.RUNNING,
        ControlState.PAUSED,
        ControlState.FINISHING,
        ControlState.STOPPED,
    ):
        s = s.to(target)
    assert s.state is ControlState.STOPPED


def test_illegal_control_transition_raises() -> None:
    with pytest.raises(IllegalTransition):
        ControlStatus(ControlState.RUNNING).to(ControlState.STARTING)


def test_risk_locked_cannot_jump_to_running() -> None:
    with pytest.raises(IllegalTransition):
        ControlStatus(ControlState.RISK_LOCKED, risk_locked=True).to(ControlState.RUNNING)


def test_safe_mode_exit_keeps_lock() -> None:
    s = ControlStatus(ControlState.SAFE_MODE, risk_locked=True).to(ControlState.RECONCILING)
    assert s.risk_locked is True


def test_no_transition_clears_lock() -> None:
    s = ControlStatus(ControlState.RUNNING).lock()
    for target in (ControlState.CLOSING, ControlState.SAFE_MODE, ControlState.RECONCILING):
        s = s.to(target)
        assert s.risk_locked is True


@pytest.mark.parametrize("state", list(ControlState))
def test_new_exposure_only_when_running_and_unlocked(state: ControlState) -> None:
    assert allows_new_exposure(ControlStatus(state)) is (state is ControlState.RUNNING)
    assert allows_new_exposure(ControlStatus(state, risk_locked=True)) is False


def test_priority_order() -> None:
    order = sorted(SafetyPriority, reverse=True)
    assert order == [
        SafetyPriority.EMERGENCY_OR_RISK,
        SafetyPriority.CLOSE_IN_PROGRESS,
        SafetyPriority.PROFIT_EXIT,
        SafetyPriority.RESTRICTING_CONTROL,
        SafetyPriority.ENTRY_OR_RECOVERY,
    ]


def test_cycle_lifecycle() -> None:
    s = CycleState.IDLE
    for target in (CycleState.ACTIVE, CycleState.CLOSING, CycleState.CLOSED):
        s = next_cycle_state(s, target)
    assert s is CycleState.CLOSED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CycleState.ACTIVE, CycleState.CLOSED),  # close is never atomic
        (CycleState.CLOSED, CycleState.ACTIVE),  # terminal
        (CycleState.CLOSING, CycleState.ACTIVE),  # no reopening while closing
    ],
)
def test_illegal_cycle_transitions(current: CycleState, target: CycleState) -> None:
    with pytest.raises(IllegalTransition):
        next_cycle_state(current, target)


def test_entering_risk_locked_sets_the_lock() -> None:
    s = ControlStatus(ControlState.RUNNING).to(ControlState.RISK_LOCKED)
    assert s.risk_locked is True


def test_locked_bot_cannot_return_to_running_without_unlock() -> None:
    s = (
        ControlStatus(ControlState.RUNNING)
        .to(ControlState.RISK_LOCKED)
        .to(ControlState.RECONCILING)
    )
    with pytest.raises(IllegalTransition, match="risk lock"):
        s.to(ControlState.RUNNING)
    assert s.unlock().to(ControlState.RUNNING).state is ControlState.RUNNING


@pytest.mark.parametrize("state", [ControlState.STARTING, ControlState.RECONCILING])
def test_emergency_close_reachable_during_startup(state: ControlState) -> None:
    assert ControlStatus(state).to(ControlState.CLOSING).state is ControlState.CLOSING
