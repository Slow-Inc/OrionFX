"""Closed vocabularies used across modules. Names follow UBIQUITOUS_LANGUAGE.md."""

from enum import StrEnum


class Mode(StrEnum):
    """Account operating mode — isolated from control and cycle lifecycle (§9.1)."""

    SHADOW = "SHADOW"
    DEMO = "DEMO"
    LIVE = "LIVE"


class ControlState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHING = "FINISHING"
    CLOSING = "CLOSING"
    RISK_LOCKED = "RISK_LOCKED"
    SAFE_MODE = "SAFE_MODE"


class CycleState(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class ControlCommand(StrEnum):
    """The five control commands (§4.5)."""

    START = "START"
    PAUSE = "PAUSE"
    FINISH_CYCLE = "FINISH_CYCLE"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    APPROVE_RECOVERY = "APPROVE_RECOVERY"


class CommandStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"
    UNRESOLVED = "UNRESOLVED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class IntentAction(StrEnum):
    OPEN = "OPEN"
    ADD = "ADD"
    CLOSE = "CLOSE"
    CLOSE_ALL = "CLOSE_ALL"


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class ExecutionState(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class Timeframe(StrEnum):
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"


class DecisionAction(StrEnum):
    ENTER = "ENTER"
    ADD = "ADD"
    EXIT = "EXIT"
    WAIT = "WAIT"


class CloseReason(StrEnum):
    TARGET_TRIGGERED = "TARGET_TRIGGERED"
    TRAILING_FLOOR = "TRAILING_FLOOR"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    RISK_LIMIT = "RISK_LIMIT"
    FINISH_CYCLE = "FINISH_CYCLE"
    MANUAL_RECONCILED = "MANUAL_RECONCILED"


class DataQuality(StrEnum):
    OK = "OK"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    MISSING = "MISSING"


class HealthStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class ForecastLineage(StrEnum):
    """Chart-only and news-enhanced forecasts keep separate lineage (PRE-04, AT-17)."""

    CHART_ONLY = "CHART_ONLY"
    NEWS_ENHANCED = "NEWS_ENHANCED"


class ModelStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    CHAMPION = "CHAMPION"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"
