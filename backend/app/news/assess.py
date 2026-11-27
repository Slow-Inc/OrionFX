"""Structured news assessment with degraded mode (#39; LLM-03, LLM-04, LLM-05).

Steps: drop expired items; flag and exclude injection attempts; ask the provider
for a strict JSON answer; validate it (schema, ranges, and cited ids must be a
subset of the ids actually given); report conflicting item directions as NEUTRAL
with capped confidence. A timeout or provider error is retried once, then the
result is degraded: no assessment, a `health.updated` saying why. That is the
safe WAIT path; it never touches risk or execution. No provider means DOWN and
no claim of news analysis.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationError

from app.core.contracts import Confidence, ContractModel, HealthUpdated, NewsAssessment
from app.core.enums import Direction, HealthStatus
from app.core.ids import new_id
from app.core.types import Probability
from app.news.injection import PROMPT_VERSION, build_prompt, injection_flags
from app.news.provider import Provider, ProviderError
from app.news.sources import NewsItem

__all__ = ["PROMPT_VERSION", "AssessmentResult", "assess_news"]

COMPONENT = "M03"
TIMEOUT_S = 20.0  # [PROPOSED]
ATTEMPTS = 2  # one retry [PROPOSED]
CONFLICT_CONFIDENCE_CAP = Decimal("0.3")  # [PROPOSED]
SEMANTICS = "uncalibrated LLM confidence in the news-implied XAUUSD direction"


class _Answer(ContractModel):
    direction: Direction
    impact: Probability
    confidence: Probability
    cited_ids: list[str] = Field(min_length=1)
    item_directions: dict[str, Direction]
    rationale: str = Field(min_length=1)


@dataclass(frozen=True)
class AssessmentResult:
    assessment: NewsAssessment | None
    health: HealthUpdated
    excluded: list[str] = field(default_factory=list)
    reason: str | None = None
    provider: str | None = None
    prompt_version: str = PROMPT_VERSION


def _health(now: datetime, status: HealthStatus, detail: str | None = None) -> HealthUpdated:
    return HealthUpdated(component=COMPONENT, status=status, last_heartbeat=now, detail=detail)


def _parse(text: str, given: set[str]) -> _Answer:
    # Numbers are parsed as Decimal: model scores never pass through binary floats.
    answer = _Answer.model_validate(json.loads(text, parse_float=Decimal, parse_int=Decimal))
    invented = set(answer.cited_ids) - given
    if invented or not set(answer.item_directions) <= given:
        raise ValueError(f"cites items it was not given: {sorted(invented)}")
    return answer


def assess_news(
    items: Sequence[NewsItem],
    provider: Provider | None,
    *,
    now: datetime,
    timeout_s: float = TIMEOUT_S,
) -> AssessmentResult:
    live = [i for i in items if i.valid_until > now]
    excluded = [i.item_id for i in live if injection_flags(f"{i.title}\n{i.body}")]
    usable = [i for i in live if i.item_id not in excluded]

    def result(
        status: HealthStatus, reason: str | None, a: NewsAssessment | None = None
    ) -> AssessmentResult:
        pid = None if provider is None else provider.provider_id
        return AssessmentResult(a, _health(now, status, reason), excluded, reason, pid)

    if provider is None:
        return result(HealthStatus.DOWN, "no LLM provider configured")
    if not usable:
        return result(HealthStatus.OK, "no usable news")

    prompt = build_prompt(usable)
    given = {i.item_id for i in usable}
    failure: Literal["provider", "output"] = "provider"
    detail = ""
    for _ in range(ATTEMPTS):
        try:
            answer = _parse(provider.complete(prompt, timeout_s=timeout_s), given)
        except ProviderError as exc:
            failure, detail = "provider", repr(exc)
            continue
        except (ValueError, ValidationError) as exc:  # json errors are ValueErrors
            failure, detail = "output", str(exc).splitlines()[0]
            continue
        cited = [i for i in usable if i.item_id in answer.cited_ids]
        stances = set(answer.item_directions.values()) - {Direction.NEUTRAL}
        conflict = len(stances) > 1
        assessment = NewsAssessment(
            assessment_id=new_id("news"),
            news_ids=[i.item_id for i in cited],
            source_refs=[i.url for i in cited],
            direction=Direction.NEUTRAL if conflict else answer.direction,
            impact=answer.impact,
            score=Confidence(
                value=min(answer.confidence, CONFLICT_CONFIDENCE_CAP)
                if conflict
                else answer.confidence,
                semantics=SEMANTICS,
            ),
            available_at=now,
            valid_until=min(i.valid_until for i in cited),
        )
        return result(HealthStatus.OK, "conflicting news" if conflict else None, assessment)
    return result(HealthStatus.DEGRADED, f"{failure} failed after {ATTEMPTS} attempts: {detail}")
