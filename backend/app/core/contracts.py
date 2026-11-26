"""Payload schemas for every event in the master plan §8.2 catalog.

`EVENT_PAYLOADS` is the single source of truth for event types; a contract test
derives the expected set from the master plan table and compares.
"""

from typing import Self

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


class LlmReasoning(ContractModel):
    reasoning_id: str
    news_assessment_ids: list[str]
    prediction_ids: list[str]
    direction: Direction
    score: Confidence
    rationale: str
    prompt_version: str
    provider: str
    valid_until: UtcDatetime


# --- M04 prediction --------------------------------------------------------


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


EVENT_PAYLOADS: dict[str, type[ContractModel]] = {
    "market.snapshot": MarketSnapshot,
    "candle.closed": CandleClosed,
    "feature.snapshot": FeatureSnapshot,
    "news.assessment": NewsAssessment,
    "prediction.generated": PredictionGenerated,
    "llm.reasoning": LlmReasoning,
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
}
