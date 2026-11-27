"""Structured news assessment with degraded mode (#39; LLM-03, LLM-04, LLM-05).

Steps: keep only items known at `now` and not expired; flag and exclude
injection attempts; ask the provider for a strict JSON answer; validate it
(schema, ranges, and every id it mentions must be one it was given); report
conflicting stances among the cited items as NEUTRAL with capped confidence.
A provider failure or an invalid answer is retried once, then the result is
degraded: no assessment and a `health.updated` saying why (never the provider's
raw error text, which may carry request details). That is the safe WAIT path;
it never touches risk or execution. No provider means DOWN and no claim of news
analysis. The answer format the prompt asks for and the model that validates it
live together here, under one prompt version.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.core.contracts import Confidence, ContractModel, HealthUpdated, NewsAssessment
from app.core.enums import Direction, HealthStatus
from app.core.ids import new_id
from app.core.types import Probability
from app.news.injection import PROMPT_VERSION, build_prompt, injection_flags
from app.news.llm_call import TIMEOUT_S, ask, load_json
from app.news.provider import Provider
from app.news.sources import NewsItem

__all__ = ["ANSWER_FORMAT", "PROMPT_VERSION", "AssessmentResult", "assess_news"]

COMPONENT = "M03"
CONFLICT_CONFIDENCE_CAP = Decimal("0.3")  # [PROPOSED]
SEMANTICS = "uncalibrated LLM confidence in the news-implied XAUUSD direction"
ANSWER_FORMAT = (
    '{"direction": "UP"|"DOWN"|"NEUTRAL", "impact": 0..1, "confidence": 0..1,\n'
    ' "cited_ids": [ids you relied on], "item_directions": {id: "UP"|"DOWN"|"NEUTRAL"},\n'
    ' "rationale": "short reason"}'
)


class _Answer(ContractModel):
    direction: Direction
    impact: Probability
    confidence: Probability
    cited_ids: list[str] = Field(min_length=1)
    item_directions: dict[str, Direction]
    rationale: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    assessment: NewsAssessment | None
    health: HealthUpdated
    excluded: tuple[str, ...] = ()
    provider: str | None = None
    prompt_version: str = PROMPT_VERSION
    """Carried with the provider id so every result can be audited and reproduced."""


def _parse(text: str, given: set[str]) -> _Answer:
    answer = _Answer.model_validate(load_json(text))
    unknown = (set(answer.cited_ids) | set(answer.item_directions)) - given
    if unknown:
        raise ValueError(f"mentions items it was not given: {sorted(unknown)}")
    return answer


def assess_news(
    items: Sequence[NewsItem],
    provider: Provider | None,
    *,
    now: datetime,
    timeout_s: float = TIMEOUT_S,
) -> AssessmentResult:
    known = [i for i in items if i.fetched_at <= now < i.valid_until]  # no look-ahead
    excluded = tuple(i.item_id for i in known if injection_flags(f"{i.title}\n{i.body}"))
    usable = [i for i in known if i.item_id not in excluded]
    pid = None if provider is None else provider.provider_id

    def done(
        status: HealthStatus, detail: str | None, assessment: NewsAssessment | None = None
    ) -> AssessmentResult:
        health = HealthUpdated(
            component=COMPONENT, status=status, last_heartbeat=now, detail=detail
        )
        return AssessmentResult(
            assessment=assessment, health=health, excluded=excluded, provider=pid
        )

    if provider is None:
        return done(HealthStatus.DOWN, "no LLM provider configured")
    if not usable:
        if excluded:
            return done(HealthStatus.DEGRADED, f"all {len(excluded)} items flagged as injection")
        return done(HealthStatus.OK, "no usable news")

    given = {i.item_id for i in usable}
    answer, detail = ask(
        provider,
        build_prompt(usable, ANSWER_FORMAT),
        lambda text: _parse(text, given),
        timeout_s=timeout_s,
    )
    if answer is None:
        return done(HealthStatus.DEGRADED, detail)
    cited = [i for i in usable if i.item_id in answer.cited_ids]
    stances = {answer.item_directions.get(i.item_id) for i in cited} - {
        None,
        Direction.NEUTRAL,
    }
    conflict = len(stances) > 1
    confidence = answer.confidence
    if conflict:
        confidence = min(confidence, CONFLICT_CONFIDENCE_CAP)
    assessment = NewsAssessment(
        assessment_id=new_id("news"),
        news_ids=[i.item_id for i in cited],
        source_refs=[i.url for i in cited],
        direction=Direction.NEUTRAL if conflict else answer.direction,
        impact=answer.impact,
        score=Confidence(value=confidence, semantics=SEMANTICS),
        available_at=now,
        valid_until=min(i.valid_until for i in cited),
    )
    return done(HealthStatus.OK, "conflicting news" if conflict else None, assessment)
