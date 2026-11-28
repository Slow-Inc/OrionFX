"""Payload schemas for every event in the master plan §8.2 catalog.

`EVENT_PAYLOADS` is the single source of truth for event types; a contract test
derives the expected set from the master plan table and compares.
"""

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    CloseReason,
    CommandStatus,
    ControlCommand,
    ControlState,
    CycleState,
    DataQuality,
    DecisionAction,
    Direction,
    Eligibility,
    ExecutionState,
    ForecastLineage,
    HealthStatus,
    IntentAction,
    Mode,
    ModelStatus,
    Side,
    Timeframe,
    Verdict,
)
from app.core.types import (
    Dec,
    Lot,
    Money,
    NonNegative,
    Percent,
    PositiveMoney,
    Price,
    Probability,
    UtcDatetime,
)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def require_window(available_at: datetime, valid_until: datetime) -> None:
    """An observation is usable over [available_at, valid_until) (core.time.live), so a
    window that ends when (or before) it starts is never usable: refuse it."""
    if available_at >= valid_until:
        raise ValueError("available_at must be before valid_until")


class Confidence(ContractModel):
    """A 0-1 score is never shipped without what it means (master plan §4.6).

    `semantics` names the target and method, e.g. "calibrated P(close up) over 3 H1 bars"
    or "uncalibrated LLM score". An LLM score is not a calibrated probability.
    """

    value: Probability
    semantics: str = Field(min_length=1)
    calibrated: bool = False


# --- M02 market data -------------------------------------------------------


class MarketSnapshot(ContractModel):
    symbol: str
    bid: Price | None
    ask: Price | None
    spread: NonNegative | None
    tick_time: UtcDatetime | None
    broker_time_offset_minutes: int | None = None
    quality: DataQuality

    @model_validator(mode="after")
    def _ask_not_below_bid(self) -> Self:
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ValueError("ask below bid")
        return self


class CandleClosed(ContractModel):
    symbol: str
    timeframe: Timeframe
    open_time: UtcDatetime
    close_time: UtcDatetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: NonNegative | None
    spread: NonNegative | None = None
    available_at: UtcDatetime
    quality_flags: list[str] = Field(default_factory=list)
    """e.g. GAP: the bar contains or follows missing data (DAT-04)."""

    @model_validator(mode="after")
    def _consistent_bar(self) -> Self:
        if not (self.low <= min(self.open, self.close) and max(self.open, self.close) <= self.high):
            raise ValueError("OHLC out of range: need low <= open, close <= high")
        if not (self.open_time < self.close_time <= self.available_at):
            raise ValueError("need open_time < close_time <= available_at (no look-ahead)")
        return self


class FeatureSnapshot(ContractModel):
    symbol: str
    feature_schema: str
    values: dict[str, Dec | None]
    quality: DataQuality
    as_of: UtcDatetime
    available_at: UtcDatetime
    valid_until: UtcDatetime

    @model_validator(mode="after")
    def _ordered_times(self) -> Self:
        if not (self.as_of <= self.available_at <= self.valid_until):
            raise ValueError("need as_of <= available_at <= valid_until")
        return self

    @property
    def ref(self) -> str:
        """`{feature_schema}@{as_of}`: how other modules cite this snapshot."""
        return f"{self.feature_schema}@{self.as_of.isoformat()}"


# --- M03 news & LLM --------------------------------------------------------


class NewsAssessment(ContractModel):
    assessment_id: str
    news_ids: list[str]
    source_refs: list[str]
    direction: Direction
    impact: Probability
    score: Confidence
    available_at: UtcDatetime
    valid_until: UtcDatetime

    @model_validator(mode="after")
    def _window(self) -> Self:
        require_window(self.available_at, self.valid_until)
        return self


class LlmReasoning(ContractModel):
    reasoning_id: str
    news_assessment_ids: list[str]
    prediction_ids: list[str]
    direction: Direction
    score: Confidence
    rationale: str
    prompt_version: str
    provider: str
    available_at: UtcDatetime
    """When it was produced: a consumer may use it only from then on (AT-15, #83)."""
    valid_until: UtcDatetime
    feature_snapshot_ref: str | None = None
    """`{feature_schema}@{as_of}` of the chart summary used, if any (auditability)."""

    @model_validator(mode="after")
    def _window(self) -> Self:
        require_window(self.available_at, self.valid_until)
        return self


# --- M04 prediction --------------------------------------------------------


_CLASS_SUM_TOLERANCE = Decimal("1e-9")


class ClassProbabilities(ContractModel):
    """Calibrated probabilities of the triple-barrier label's three outcomes (#129) for
    the bet in the forecast's `direction`: that side's label, whose TP outcome is the
    direction itself (a long's TP is UP, a short's TP is DOWN) and whose time barrier is
    FLAT. They sum to 1. `label_version` names the label they were calibrated on."""

    label_version: str = Field(min_length=1)
    up: Probability
    down: Probability
    flat: Probability

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        if abs(self.up + self.down + self.flat - 1) > _CLASS_SUM_TOLERANCE:
            raise ValueError("class probabilities must sum to 1")
        return self


class PredictionGenerated(ContractModel):
    prediction_id: str
    model_id: str
    model_version: str
    lineage: ForecastLineage
    horizon: Timeframe
    horizon_bars: int = Field(gt=0)
    direction: Direction
    probability: Confidence
    expected_range_low: Price | None = None
    expected_range_high: Price | None = None
    data_quality: DataQuality
    input_refs: list[str]
    generated_at: UtcDatetime
    valid_until: UtcDatetime
    class_probabilities: ClassProbabilities | None = None
    """Only from a calibrated three-class model (#129); M05 prices EV from them and
    refuses EV without them."""

    @model_validator(mode="after")
    def _ordered_range(self) -> Self:
        lo, hi = self.expected_range_low, self.expected_range_high
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("expected_range_low above expected_range_high")
        return self


# --- M05 decision ----------------------------------------------------------


class DecisionCreated(ContractModel):
    decision_id: str
    action: DecisionAction
    strategy_id: str | None
    side: Side | None
    expected_value_usd: Money | None
    confidence: Confidence | None
    reason_codes: list[str]
    evidence_refs: list[str]
    policy_version: str


class TradeIntent(ContractModel):
    """A proposal to trade — never a broker order (§8.3)."""

    intent_id: str
    decision_id: str
    action: IntentAction
    side: Side | None
    symbol: str
    requested_lot: Lot | None
    strategy_id: str
    cycle_id: str | None
    mode: Mode
    expected_value_usd: Money | None
    prediction_ids: list[str]
    position_refs: list[str] = Field(default_factory=list)
    valid_until: UtcDatetime

    @model_validator(mode="after")
    def _sized_when_adding_exposure(self) -> Self:
        adds = self.action in (IntentAction.OPEN, IntentAction.ADD)
        if adds and (self.side is None or self.requested_lot is None):
            raise ValueError(f"{self.action} needs side and requested_lot")
        return self


# --- M06 risk & execution --------------------------------------------------


class RiskDecision(ContractModel):
    intent_id: str
    verdict: Verdict
    approved_lot: Lot | None = None
    policy_version: str
    reason_codes: list[str]
    decided_at: UtcDatetime
    valid_until: UtcDatetime | None = None

    @model_validator(mode="after")
    def _bounded_approval(self) -> Self:
        """An approval is never open-ended (§8.3); a denial approves nothing."""
        if self.verdict is Verdict.ALLOW and (
            self.approved_lot is None or self.valid_until is None
        ):
            raise ValueError("ALLOW needs approved_lot and valid_until")
        if self.verdict is Verdict.DENY and self.approved_lot is not None:
            raise ValueError("DENY must not carry approved_lot")
        return self


class ExecutionUpdated(ContractModel):
    request_id: str
    intent_id: str | None
    state: ExecutionState
    broker_order_id: str | None = None
    broker_position_id: str | None = None
    broker_deal_ids: list[str] = Field(default_factory=list)
    filled_lot: NonNegative | None = None
    fill_price: Price | None = None
    error: str | None = None


class AccountSnapshot(ContractModel):
    currency: str
    balance: Money | None
    equity: Money | None
    margin: NonNegative | None
    free_margin: Money | None
    connected: bool
    quote_status: DataQuality
    observed_at: UtcDatetime


class BotPosition(ContractModel):
    """One of the bot's own positions (ownership-checked; never a Manual Position)."""

    position_id: str
    symbol: str
    side: Side
    volume: Lot
    open_price: Price
    profit: Money
    commission: Money
    swap: Money
    cycle_id: str | None


SNAPSHOT_STALE_AFTER = timedelta(seconds=60)
"""An account or positions snapshot older than this no longer describes the account
(M06 publishes one at least every 30 s while connected)."""


class PositionsSnapshot(ContractModel):
    items: list[BotPosition]
    observed_at: UtcDatetime


class CycleUpdated(ContractModel):
    state: CycleState
    target_usd: PositiveMoney
    cycle_net_estimated_usd: Money | None
    peak_net_usd: Money | None
    trailing_floor_usd: Money | None
    trailing_active: bool
    final_net_usd: Money | None = None
    close_reason: CloseReason | None = None
    target_triggered: bool = False
    target_achieved: bool | None = None
    unresolved: list[str] = Field(default_factory=list)


class RiskUpdated(ContractModel):
    epoch_dd_percent: Percent | None
    lifetime_dd_percent: Percent | None
    epoch_ceiling_percent: Percent
    lifetime_ceiling_percent: Percent
    risk_locked: bool
    reason_codes: list[str]
    policy_version: str


# --- M08 control -----------------------------------------------------------


class ControlRequested(ContractModel):
    command_id: str
    command: ControlCommand
    actor: str
    expected_state_version: int | None = None


class ControlCompleted(ContractModel):
    command_id: str
    status: CommandStatus
    resulting_state: ControlState
    risk_locked: bool
    unresolved: list[str] = Field(default_factory=list)
    reason: str | None = None


# --- M07 models ------------------------------------------------------------


class ModelCandidate(ContractModel):
    model_id: str
    model_version: str
    dataset_refs: list[str]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation: dict[str, Dec | None]
    status: ModelStatus = ModelStatus.CANDIDATE


class ModelApproved(ContractModel):
    model_id: str
    model_version: str
    approval_ref: str
    evidence_refs: list[str] = Field(min_length=1)
    policy_ref: str
    allowed_modes: list[Mode] = Field(min_length=1)


# --- cross-cutting ---------------------------------------------------------


class HealthUpdated(ContractModel):
    component: str
    status: HealthStatus
    last_heartbeat: UtcDatetime
    detail: str | None = None


# --- V1.1 strategy & technique contracts (§15.5) [PROPOSED schema] ----------------------

CatalogId = Annotated[str, Field(pattern=r"^CAT-[A-Z]\d{2}-\d{2}$")]
Sha256Ref = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
"""Prefixed hash for V1.1 artifacts (ModelCandidate predates it and stores bare hex)."""
SubjectKind = Literal["strategy", "technique", "composition", "model"]


def sha256_ref(data: bytes) -> str:
    """The `Sha256Ref` form of an artifact's bytes."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class ParameterBound(ContractModel):
    low: Dec
    high: Dec

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low > self.high:
            raise ValueError("parameter bound low > high")
        return self


class StrategyManifest(ContractModel):
    """A versioned, hashed strategy artifact. Never executable by itself."""

    strategy_id: str = Field(pattern=r"^[^@\s]+$")
    version: str = Field(pattern=r"^[^@\s]+$")
    """Neither may contain '@': `id@version` is the subject ref."""
    artifact_hash: Sha256Ref
    family: str
    catalog_ids: list[CatalogId] = Field(min_length=1)
    technique_refs: list[str]
    timeframes: list[Timeframe] = Field(min_length=1)
    warmup_bars: int = Field(ge=0)
    parameters: dict[str, ParameterBound]
    risk_tags: list[str]
    supported_modes: list[Mode] = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    max_latency_ms: int | None = Field(default=None, gt=0)


class TechniqueObservation(ContractModel):
    technique_id: str
    technique_version: str
    input_refs: list[str]
    available_at: UtcDatetime
    valid_until: UtcDatetime
    values: dict[str, Dec | None]
    unit: str
    uncertainty: Dec | None
    quality_flags: list[str]
    reason_codes: list[str]

    @model_validator(mode="after")
    def _window(self) -> Self:
        require_window(self.available_at, self.valid_until)
        return self


class StrategyProposal(ContractModel):
    """What M05 proposes; M06 decides. Never a broker order (§4.6)."""

    proposal_id: str
    strategy_id: str
    strategy_version: str
    technique_refs: list[str]
    composition_id: str | None
    decision_id: str | None
    """None until a decision adopts the proposal."""
    direction: Direction
    action: IntentAction
    horizon: Timeframe
    expected_value_usd: Money | None
    cost_estimate_usd: NonNegative | None
    requested_lot: Lot | None
    valid_until: UtcDatetime
    rationale: str
    constraints: list[str]

    @model_validator(mode="after")
    def _entries_are_directed_and_sized(self) -> Self:
        if self.action in (IntentAction.OPEN, IntentAction.ADD) and (
            self.direction is Direction.NEUTRAL or self.requested_lot is None
        ):
            raise ValueError(f"{self.action} needs an UP/DOWN direction and a requested lot")
        return self


ELIGIBILITY_MODES: dict[Eligibility, frozenset[Mode]] = {
    Eligibility.DISABLED: frozenset(),
    Eligibility.SHADOW_ONLY: frozenset({Mode.SHADOW}),
    Eligibility.DEMO_ELIGIBLE: frozenset({Mode.SHADOW, Mode.DEMO}),
    Eligibility.LIVE_ELIGIBLE: frozenset({Mode.SHADOW, Mode.DEMO, Mode.LIVE}),
}
"""The modes each eligibility tier may approve (§16.4), lowest tier first."""


def minimum_eligibility(modes: list[Mode]) -> Eligibility:
    """The lowest tier that allows every mode in `modes`."""
    for tier, allowed in ELIGIBILITY_MODES.items():
        if set(modes) <= allowed:
            return tier
    raise ValueError(f"no eligibility tier allows {sorted(modes)}")


class EligibilityRecord(ContractModel):
    subject_hash: Sha256Ref
    subject_kind: SubjectKind
    subject_ref: str
    user_enabled: bool
    eligibility: Eligibility
    approved_modes: list[Mode]
    context: dict[str, str]
    evidence_refs: list[str]
    policy_version: str = Field(min_length=1)
    expires_at: UtcDatetime | None
    revoked_at: UtcDatetime | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        modes = set(self.approved_modes)
        if (self.revoked_at is not None or not self.user_enabled) and (
            self.eligibility is not Eligibility.DISABLED
        ):
            raise ValueError("a revoked or owner-disabled subject is DISABLED")
        allowed = ELIGIBILITY_MODES[self.eligibility]
        if not modes <= allowed:
            raise ValueError(f"{self.eligibility} does not allow {sorted(modes - allowed)}")
        if self.eligibility is Eligibility.LIVE_ELIGIBLE and not self.evidence_refs:
            raise ValueError("live eligibility needs evidence refs")
        return self

    def approves(self, mode: Mode, now: datetime) -> bool:
        """The one rule for "approved for this mode at this time"."""
        unexpired = self.expires_at is None or now < self.expires_at
        return self.user_enabled and unexpired and mode in self.approved_modes


EVENT_PAYLOADS: dict[str, type[ContractModel]] = {
    "market.snapshot": MarketSnapshot,
    "candle.closed": CandleClosed,
    "feature.snapshot": FeatureSnapshot,
    "news.assessment": NewsAssessment,
    "prediction.generated": PredictionGenerated,
    "llm.reasoning": LlmReasoning,
    "positions.snapshot": PositionsSnapshot,
    "decision.created": DecisionCreated,
    "trade.intent": TradeIntent,
    "risk.decision": RiskDecision,
    "execution.updated": ExecutionUpdated,
    "account.snapshot": AccountSnapshot,
    "cycle.updated": CycleUpdated,
    "risk.updated": RiskUpdated,
    "control.requested": ControlRequested,
    "control.completed": ControlCompleted,
    "model.candidate": ModelCandidate,
    "model.approved": ModelApproved,
    "health.updated": HealthUpdated,
    "technique.observed": TechniqueObservation,
    "strategy.proposed": StrategyProposal,
    "strategy.candidate": StrategyManifest,
    "strategy.eligibility.updated": EligibilityRecord,
}
