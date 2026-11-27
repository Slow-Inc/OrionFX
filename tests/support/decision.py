"""Shared builders for the M05 decision tests: a feature snapshot context, proposals,
forecasts, news, reasoning, costs and an empty cycle. A support module, not a test
module, so tests import it instead of importing each other."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.contracts import (
    Confidence,
    FeatureSnapshot,
    LlmReasoning,
    NewsAssessment,
    PredictionGenerated,
    StrategyProposal,
)
from app.core.enums import DataQuality, Direction, ForecastLineage, IntentAction, Mode, Timeframe
from app.decision.context import DecisionContext
from app.decision.fusion import Costs, CycleView
from app.decision.strategies import ATR_M15

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
LATER = T0 + timedelta(minutes=5)


def _ctx(**over: object) -> DecisionContext:
    snap = FeatureSnapshot(
        symbol="XAUUSD",
        feature_schema="m02.features.v1",
        values={ATR_M15: D(4)},
        quality=DataQuality.OK,
        as_of=T0,
        available_at=T0 + timedelta(seconds=1),
        valid_until=LATER,
    )
    base = DecisionContext(
        now=NOW, mode=Mode.DEMO, snapshot=snap, price=D(2000), spread=D("0.20"),
        initial_lot=D("0.01"),
    )  # fmt: skip
    return replace(base, **over)  # type: ignore[arg-type]


def _proposal(
    sid: str = "trend_following", direction: Direction = Direction.UP
) -> StrategyProposal:
    return StrategyProposal(
        proposal_id=f"prop_{sid}", strategy_id=sid, strategy_version="1.0.0",
        technique_refs=["k"], composition_id=None, decision_id=None, direction=direction,
        action=IntentAction.OPEN, horizon=Timeframe.H1, expected_value_usd=None,
        cost_estimate_usd=None, requested_lot=D("0.01"), valid_until=LATER, rationale="r",
        constraints=[],
    )  # fmt: skip


def _forecast(
    p: str = "0.70",
    direction: Direction = Direction.UP,
    calibrated: bool = True,
    valid_until: datetime = LATER,
    lineage: ForecastLineage = ForecastLineage.CHART_ONLY,
) -> PredictionGenerated:
    return PredictionGenerated(
        prediction_id="pred_1", model_id="m", model_version="1", lineage=lineage,
        horizon=Timeframe.H1, horizon_bars=1, direction=direction,
        probability=Confidence(value=D(p), semantics="calibrated P(dir) 1 H1 bar",
                               calibrated=calibrated),
        data_quality=DataQuality.OK, input_refs=["snap"], generated_at=T0,
        valid_until=valid_until,
    )  # fmt: skip


def _news(
    direction: Direction = Direction.DOWN,
    cited: bool = True,
    impact: str = "0.8",
    aid: str = "na_1",
    valid_until: datetime = LATER,
) -> NewsAssessment:
    return NewsAssessment(
        assessment_id=aid, news_ids=["n1"], source_refs=["src"] if cited else [],
        direction=direction, impact=D(impact),
        score=Confidence(value=D("0.6"), semantics="uncalibrated LLM score"),
        available_at=T0, valid_until=valid_until,
    )  # fmt: skip


def _reasoning(direction: Direction, cites: list[str]) -> LlmReasoning:
    return LlmReasoning(
        reasoning_id="rs_1", news_assessment_ids=cites, prediction_ids=[], direction=direction,
        score=Confidence(value=D("0.6"), semantics="uncalibrated LLM score"), rationale="r",
        prompt_version="v", provider="fake", available_at=T0, valid_until=LATER,
    )  # fmt: skip


COSTS = Costs(contract_size=D(100), commission_per_lot=D("3.5"))
CYCLE = CycleView(cycle_id="cyc_1", open_positions=0, last_closed_at=None, pending=frozenset())
