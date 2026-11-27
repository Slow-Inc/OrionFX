"""Closed vocabularies used across modules. Names follow UBIQUITOUS_LANGUAGE.md."""

from enum import StrEnum


class Mode(StrEnum):
    """Account operating mode — isolated from control and cycle lifecycle (§9.1)."""

    SHADOW = "SHADOW"
    DEMO = "DEMO"
    LIVE = "LIVE"


class RiskProfile(StrEnum):
    """Profiles sit under the owner's max loss (RSK-09); Live starts LOW."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


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


class RequestKind(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CANCEL = "CANCEL"


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


class WaitReason(StrEnum):
    """Why M05 decided WAIT (DEC-04); the decision's first reason code."""

    STALE_INPUT = "STALE_INPUT"
    INVALID_LOT = "INVALID_LOT"
    NO_OPPORTUNITY = "NO_OPPORTUNITY"
    CONFLICT_STRATEGIES = "CONFLICT_STRATEGIES"
    POSITION_OPEN = "POSITION_OPEN"
    COOLDOWN = "COOLDOWN"
    NEWS_VETO = "NEWS_VETO"
    CONFLICT_EVIDENCE = "CONFLICT_EVIDENCE"
    EV_UNKNOWN = "EV_UNKNOWN"
    EV_NOT_POSITIVE = "EV_NOT_POSITIVE"
    DUPLICATE = "DUPLICATE"
    HOLD = "HOLD"
    CAP_REACHED = "CAP_REACHED"
    CYCLE_NET_UNKNOWN = "CYCLE_NET_UNKNOWN"
    EXIT_PENDING = "EXIT_PENDING"
    NO_LIVE_RECOVERY = "NO_LIVE_RECOVERY"


class CloseReason(StrEnum):
    TARGET_TRIGGERED = "TARGET_TRIGGERED"
    TRAILING_FLOOR = "TRAILING_FLOOR"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    RISK_LIMIT = "RISK_LIMIT"
    FINISH_CYCLE = "FINISH_CYCLE"
    MANUAL_RECONCILED = "MANUAL_RECONCILED"
    AI_EXIT = "AI_EXIT"
    """An allowed CLOSE_ALL from M05 ended the cycle before its target (§4.2)."""


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


class CatalogClass(StrEnum):
    """§16.2: Required, Conditional, research-only (Shadow/Demo), Excluded."""

    R = "R"
    C = "C"
    D = "D"
    X = "X"


class CoverageState(StrEnum):
    """§16.4 implementation coverage of a catalog row (not operational eligibility)."""

    PLANNED = "planned"
    BLOCKED = "blocked"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    CERTIFIED = "certified"
    SUSPENDED = "suspended"
    EXCLUDED = "excluded"


class Eligibility(StrEnum):
    """§16.4 operational eligibility, per context and version."""

    DISABLED = "disabled"
    SHADOW_ONLY = "shadow_only"
    DEMO_ELIGIBLE = "demo_eligible"
    LIVE_ELIGIBLE = "live_eligible"


class QualityFlag(StrEnum):
    """Data-quality flags on candles and technique observations (UST-03)."""

    GAP = "GAP"
    WARMUP = "WARMUP"
    GAP_IN_WINDOW = "GAP_IN_WINDOW"
    STALE = "STALE"
    MISSING_INPUT = "MISSING_INPUT"
    TICK_COUNT_NOT_TRADED_VOLUME = "TICK_COUNT_NOT_TRADED_VOLUME"


class EventImpact(StrEnum):
    """Economic-calendar impact of an event."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
