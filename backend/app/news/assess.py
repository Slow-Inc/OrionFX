"""Structured news assessment with degraded mode (#39; LLM-03, LLM-04, LLM-05).

Steps: keep only items known at `now` and not expired; flag and exclude
injection attempts; ask the provider for a strict JSON answer; validate it
(schema, ranges, every id it mentions must be one it was given, and every cited id
needs a stance); report conflicting stances among the cited items, or an overall
direction they do not support, as NEUTRAL with capped confidence.
A provider failure or an invalid answer is retried once, then the result is
degraded: no assessment and a `health.updated` saying why (never the provider's
raw error text, which may carry request details). That is the safe WAIT path;
it never touches risk or execution. No provider means DOWN and no claim of news
analysis. The answer format the prompt asks for and the model that validates it
live together here, under one prompt version.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.core.contracts import Confidence, ContractModel, HealthUpdated, NewsAssessment
from app.core.enums import Direction, HealthStatus
from app.core.ids import new_id
from app.core.time import Clock, live
from app.core.types import Probability
from app.news.injection import PROMPT_VERSION, build_prompt, injection_flags
from app.news.llm_call import NO_PROVIDER, TIMEOUT_S, ask, health, load_json
from app.news.provider import Provider
from app.news.sources import NewsItem

__all__ = ["ANSWER_FORMAT", "PROMPT_VERSION", "AssessmentResult", "assess_news"]

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
        raise ValueError(f"mentions {len(unknown)} item id(s) it was not given")
    unstated = set(answer.cited_ids) - set(answer.item_directions)
    if unstated:
        raise ValueError(f"gives no stance for {len(unstated)} cited item id(s)")
    return answer


def _handles(items: Sequence[NewsItem]) -> dict[str, NewsItem]:
    """The id each item has in the prompt, one per item (#121, MAJ-05.5). The cache keys
    items by (source, item_id), so two sources can share an item_id. The first item with
    an id keeps it; each later one, in input order, gets `@2`, `@3`, ... `@` is outside
    NewsItem's id charset, so a suffixed handle never equals a real id and each handle
    names exactly one item. The source itself is never put in the prompt."""
    seen: Counter[str] = Counter()
    handles: dict[str, NewsItem] = {}
    for i in items:
        seen[i.item_id] += 1
        n = seen[i.item_id]
        handles[i.item_id if n == 1 else f"{i.item_id}@{n}"] = i
    return handles


def assess_news(
    items: Sequence[NewsItem],
    provider: Provider | None,
    *,
    now: datetime,
    timeout_s: float = TIMEOUT_S,
    clock: Clock | None = None,
) -> AssessmentResult:
    """`now` judges which items are live; `available_at` is stamped from `clock` after the
    answer returns (defaults to `now`), so an assessment is never usable before it existed."""
    known = [i for i in items if live(i.fetched_at, i.valid_until, now)]  # no look-ahead
    flagged: list[NewsItem] = []
    usable: list[NewsItem] = []
    for i in known:
        (flagged if injection_flags(f"{i.title}\n{i.body}") else usable).append(i)
    excluded = tuple(i.item_id for i in flagged)
    pid = None if provider is None else provider.provider_id

    def done(
        status: HealthStatus, detail: str | None, assessment: NewsAssessment | None = None
    ) -> AssessmentResult:
        return AssessmentResult(
            assessment=assessment,
            health=health(status, detail, now),
            excluded=excluded,
            provider=pid,
        )

    if provider is None:
        return done(HealthStatus.DOWN, NO_PROVIDER)
    if not usable:
        if excluded:
            return done(HealthStatus.DEGRADED, f"all {len(excluded)} items flagged as injection")
        return done(HealthStatus.OK, "no usable news")

    handles = _handles(usable)
    answer, detail = ask(
        provider,
        build_prompt(usable, ANSWER_FORMAT, ids=list(handles)),
        lambda text: _parse(text, set(handles)),
        timeout_s=timeout_s,
    )
    if answer is None:
        return done(HealthStatus.DEGRADED, detail)
    cited_ids = [h for h in handles if h in answer.cited_ids]
    cited = [handles[h] for h in cited_ids]
    stances = {answer.item_directions[h] for h in cited_ids} - {Direction.NEUTRAL}
    # conflicting stances, or an overall direction its cited items do not support (#121)
    conflict = len(stances) > 1 or (
        answer.direction is not Direction.NEUTRAL and stances != {answer.direction}
    )
    confidence = answer.confidence
    if conflict:
        confidence = min(confidence, CONFLICT_CONFIDENCE_CAP)
    answered_at = clock() if clock else now
    expiry = min(i.valid_until for i in cited)
    if answered_at >= expiry:  # the cited news expired while the model was answering
        return done(HealthStatus.DEGRADED, "news expired during the call", None)
    assessment = NewsAssessment(
        assessment_id=new_id("news"),
        news_ids=[i.item_id for i in cited],
        source_refs=[i.url for i in cited],
        direction=Direction.NEUTRAL if conflict else answer.direction,
        impact=answer.impact,
        score=Confidence(value=confidence, semantics=SEMANTICS),
        available_at=answered_at,
        valid_until=expiry,
    )
    return done(HealthStatus.OK, "conflicting news" if conflict else None, assessment)
