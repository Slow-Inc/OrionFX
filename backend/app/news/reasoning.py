"""LLM reasoning stage (#40; LLM-04: link news and charts with structured, explained
output; never control execution).

Inputs are internal, already-validated contracts referenced by id: news
assessments (M03 slice B), forecasts (M04) and an M02 feature snapshot. Only
inputs known at `now` and not expired are used (no look-ahead); forecasts and
the snapshot also need usable data quality. From the snapshot, only the keys M02
publishes as its chart summary (`SUMMARY_KEYS`) go into the prompt, and only for
the schema version this stage was written against. It runs only when at least
one usable assessment exists. The answer may cite only the ids it was given;
duplicates are dropped; the result records the snapshot it used and expires with
the earliest input it relied on. Failures degrade exactly as in assessment
(shared `ask`): no reasoning, a health status, WAIT. Nothing here reaches the
broker.
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
from app.core.enums import Direction, HealthStatus
from app.core.ids import new_id
from app.core.time import Clock, live
from app.core.types import Probability
from app.market_data.features import SUMMARY_KEYS, USABLE_QUALITY, snapshot_problem
from app.news.llm_call import NO_PROVIDER, TIMEOUT_S, ask, health, load_json
from app.news.provider import Provider

__all__ = ["REASONING_PROMPT_VERSION", "ReasoningResult", "reason"]

REASONING_PROMPT_VERSION = "m03.reason.v1"
SEMANTICS = "uncalibrated LLM confidence in the combined XAUUSD direction"
USABLE = USABLE_QUALITY
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


def _usable_snapshot(snapshot: FeatureSnapshot | None, now: datetime) -> FeatureSnapshot | None:
    return None if snapshot is None or snapshot_problem(snapshot, now) else snapshot


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
    clock: Clock | None = None,
) -> ReasoningResult:
    """`now` judges which inputs are live; `available_at` is stamped from `clock` after the
    answer returns (defaults to `now`), so reasoning is never usable before it existed."""
    if provider is None:
        return ReasoningResult(None, health(HealthStatus.DOWN, NO_PROVIDER, now))
    news = [a for a in assessments if live(a.available_at, a.valid_until, now)]
    if not news:
        return ReasoningResult(
            None, health(HealthStatus.OK, "no news assessment to reason about", now)
        )
    forecasts = [
        p
        for p in predictions
        if live(p.generated_at, p.valid_until, now) and p.data_quality in USABLE
    ]
    snap = _usable_snapshot(snapshot, now)
    features = (
        {}
        if snap is None
        else {
            k: str(v) for k, v in sorted(snap.values.items()) if k in SUMMARY_KEYS and v is not None
        }
    )
    if not features:
        snap = None  # nothing from it was used
    a_ids = {a.assessment_id for a in news}
    p_ids = {p.prediction_id for p in forecasts}

    def parse(text: str) -> _Answer:
        answer = _Answer.model_validate(load_json(text))
        unknown = (set(answer.cited_assessment_ids) - a_ids) | (
            set(answer.cited_prediction_ids) - p_ids
        )
        if unknown:
            raise ValueError(f"cites {len(unknown)} id(s) it was not given")
        return answer

    answer, detail = ask(provider, _prompt(news, forecasts, features), parse, timeout_s=timeout_s)
    if answer is None:
        return ReasoningResult(None, health(HealthStatus.DEGRADED, detail, now))
    cited_a = list(dict.fromkeys(answer.cited_assessment_ids))
    cited_p = list(dict.fromkeys(answer.cited_prediction_ids))
    expiries = [a.valid_until for a in news if a.assessment_id in cited_a]
    expiries += [p.valid_until for p in forecasts if p.prediction_id in cited_p]
    if snap is not None:
        expiries.append(snap.valid_until)
    answered_at = clock() if clock else now
    if answered_at >= min(expiries):  # its evidence expired while the model was answering
        return ReasoningResult(
            None, health(HealthStatus.DEGRADED, "evidence expired during the call", now)
        )
    reasoning = LlmReasoning(
        reasoning_id=new_id("reason"),
        news_assessment_ids=cited_a,
        prediction_ids=cited_p,
        direction=answer.direction,
        score=Confidence(value=answer.confidence, semantics=SEMANTICS),
        rationale=answer.rationale,
        prompt_version=REASONING_PROMPT_VERSION,
        provider=provider.provider_id,
        available_at=answered_at,
        valid_until=min(expiries),
        feature_snapshot_ref=None if snap is None else snap.ref,
    )
    return ReasoningResult(reasoning, health(HealthStatus.OK, None, now))
