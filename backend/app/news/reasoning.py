"""LLM reasoning stage (#40; LLM-04: link news and charts with structured, explained
output; never control execution).

Inputs are internal, already-validated contracts: news assessments (M03 slice B),
forecasts (M04) and a feature snapshot (M02), referenced by id. Only summary
features (directions, alignment, regime flags) go into the prompt, and only when
the snapshot is usable (not STALE or MISSING). It runs only when at least one
unexpired assessment exists. The answer must cite only the assessment and
prediction ids it was given; the result's validity ends with the earliest
expiry among what it relied on. Failures degrade exactly as in assessment: no
reasoning, a health status, WAIT. Nothing here can reach the broker.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import Field

from app.core.contracts import (
    Confidence,
    ContractModel,
    FeatureSnapshot,
    HealthUpdated,
    LlmReasoning,
    NewsAssessment,
    PredictionGenerated,
)
from app.core.enums import DataQuality, Direction, HealthStatus
from app.core.ids import new_id
from app.core.types import Probability
from app.news.llm_call import TIMEOUT_S, ask, load_json
from app.news.provider import Provider

COMPONENT = "M03"
REASONING_PROMPT_VERSION = "m03.reason.v1"
SEMANTICS = "uncalibrated LLM confidence in the combined XAUUSD direction"
SUMMARY_SUFFIXES = (".direction", ".aligned", ".trend", ".range", ".volatile", ".structure")
USABLE = (DataQuality.OK, DataQuality.DEGRADED)
ANSWER_FORMAT = (
    '{"direction": "UP"|"DOWN"|"NEUTRAL", "confidence": 0..1,\n'
    ' "cited_assessment_ids": [...], "cited_prediction_ids": [...],\n'
    ' "rationale": "short explanation linking the evidence"}'
)


class _Answer(ContractModel):
    direction: Direction
    confidence: Probability
    cited_assessment_ids: list[str] = Field(min_length=1)
    cited_prediction_ids: list[str]
    rationale: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    reasoning: LlmReasoning | None
    health: HealthUpdated


def _summary(snapshot: FeatureSnapshot | None, now: datetime) -> dict[str, str]:
    if snapshot is None or snapshot.quality not in USABLE or snapshot.valid_until <= now:
        return {}
    return {
        k: str(v)
        for k, v in sorted(snapshot.values.items())
        if v is not None and k.endswith(SUMMARY_SUFFIXES)
    }


def _prompt(
    assessments: Sequence[NewsAssessment],
    predictions: Sequence[PredictionGenerated],
    features: dict[str, str],
) -> str:
    lines = [
        f"[{REASONING_PROMPT_VERSION}] Combine the evidence below into one view of the",
        "XAUUSD direction. Cite only ids listed here. Answer with one JSON object:",
        ANSWER_FORMAT,
        "News assessments:",
        *(
            f"- {a.assessment_id}: {a.direction} impact={a.impact} "
            f"confidence={a.score.value} ({a.score.semantics})"
            for a in assessments
        ),
        "Forecasts:",
        *(
            f"- {p.prediction_id}: {p.direction} over {p.horizon_bars}x{p.horizon} "
            f"score={p.probability.value} ({p.probability.semantics})"
            for p in predictions
        ),
        "Chart summary:",
        *(f"- {k} = {v}" for k, v in features.items()),
    ]
    return "\n".join(lines)


def reason(
    assessments: Sequence[NewsAssessment],
    predictions: Sequence[PredictionGenerated],
    snapshot: FeatureSnapshot | None,
    provider: Provider | None,
    *,
    now: datetime,
    timeout_s: float = TIMEOUT_S,
) -> ReasoningResult:
    def done(
        status: HealthStatus, detail: str | None, r: LlmReasoning | None = None
    ) -> ReasoningResult:
        health = HealthUpdated(
            component=COMPONENT, status=status, last_heartbeat=now, detail=detail
        )
        return ReasoningResult(reasoning=r, health=health)

    if provider is None:
        return done(HealthStatus.DOWN, "no LLM provider configured")
    news = [a for a in assessments if a.available_at <= now < a.valid_until]
    if not news:
        return done(HealthStatus.OK, "no news assessment to reason about")
    forecasts = [p for p in predictions if p.generated_at <= now < p.valid_until]
    features = _summary(snapshot, now)
    a_ids = {a.assessment_id for a in news}
    p_ids = {p.prediction_id for p in forecasts}

    def parse(text: str) -> _Answer:
        answer = _Answer.model_validate(load_json(text))
        unknown = (set(answer.cited_assessment_ids) - a_ids) | (
            set(answer.cited_prediction_ids) - p_ids
        )
        if unknown:
            raise ValueError(f"cites ids it was not given: {sorted(unknown)}")
        return answer

    answer, detail = ask(provider, _prompt(news, forecasts, features), parse, timeout_s=timeout_s)
    if answer is None:
        return done(HealthStatus.DEGRADED, detail)
    expiries = [a.valid_until for a in news if a.assessment_id in answer.cited_assessment_ids]
    expiries += [p.valid_until for p in forecasts if p.prediction_id in answer.cited_prediction_ids]
    if features and snapshot is not None:
        expiries.append(snapshot.valid_until)
    return done(
        HealthStatus.OK,
        None,
        LlmReasoning(
            reasoning_id=new_id("reason"),
            news_assessment_ids=answer.cited_assessment_ids,
            prediction_ids=answer.cited_prediction_ids,
            direction=answer.direction,
            score=Confidence(value=answer.confidence, semantics=SEMANTICS),
            rationale=answer.rationale,
            prompt_version=REASONING_PROMPT_VERSION,
            provider=provider.provider_id,
            valid_until=min(expiries),
        ),
    )
