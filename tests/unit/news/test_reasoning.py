"""M03 slice C: LLM reasoning stage and health (#40; LLM-04: reasoning links news and
charts with structured, explained output; it never controls execution)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.contracts import Confidence, FeatureSnapshot, NewsAssessment, PredictionGenerated
from app.core.enums import DataQuality, Direction, ForecastLineage, HealthStatus, Timeframe
from app.news.provider import FakeProvider, ProviderTimeout
from app.news.reasoning import REASONING_PROMPT_VERSION, reason

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=5)


def _assessment(i: str, *, until: timedelta = timedelta(hours=2)) -> NewsAssessment:
    return NewsAssessment(
        assessment_id=i,
        news_ids=["n1"],
        source_refs=["https://wire-a.example/n1"],
        direction=Direction.UP,
        impact=D("0.6"),
        score=Confidence(value=D("0.5"), semantics="uncalibrated LLM confidence"),
        available_at=T0,
        valid_until=T0 + until,
    )


def _prediction(i: str) -> PredictionGenerated:
    return PredictionGenerated(
        prediction_id=i,
        model_id="lgbm",
        model_version="0.0-test",
        lineage=ForecastLineage.CHART_ONLY,
        horizon=Timeframe.H1,
        horizon_bars=3,
        direction=Direction.UP,
        probability=Confidence(value=D("0.58"), semantics="test score"),
        data_quality=DataQuality.OK,
        input_refs=["snap-1"],
        generated_at=T0,
        valid_until=T0 + timedelta(hours=1),
    )


SNAPSHOT = FeatureSnapshot(
    symbol="XAUUSD",
    feature_schema="m02.features.v1",
    values={
        "H1.direction_10.direction": D(1),
        "mtf.aligned": D(1),
        "M5.sma_20.sma": D(2000),
        "H1.bar_shape.direction": D(-1),
    },
    quality=DataQuality.OK,
    as_of=T0,
    available_at=T0 + timedelta(seconds=1),
    valid_until=T0 + timedelta(minutes=30),
)


def _answer(**over: object) -> str:
    base: dict[str, object] = {
        "direction": "UP",
        "confidence": 0.5,
        "cited_assessment_ids": ["a1"],
        "cited_prediction_ids": ["p1"],
        "rationale": "Dovish news agrees with the H1 uptrend and the forecast.",
    }
    base.update(over)
    return json.dumps(base)


def test_reasoning_links_news_charts_and_forecasts() -> None:
    provider = FakeProvider([_answer()])
    result = reason([_assessment("a1")], [_prediction("p1")], SNAPSHOT, provider, now=NOW)
    r = result.reasoning
    assert r is not None
    assert r.news_assessment_ids == ["a1"] and r.prediction_ids == ["p1"]
    assert r.direction is Direction.UP and r.score.calibrated is False
    assert r.prompt_version == REASONING_PROMPT_VERSION and r.provider == "fake"
    assert r.valid_until == T0 + timedelta(minutes=30)  # the earliest expiry it relied on
    assert r.available_at == NOW  # known from the moment it was produced, not before
    prompt = provider.prompts[0]
    assert "a1" in prompt and "p1" in prompt and "H1.direction_10.direction" in prompt
    assert "M5.sma_20.sma" not in prompt  # only the summary features, not every value
    assert result.health.status is HealthStatus.OK


def test_no_assessments_means_no_call() -> None:
    provider = FakeProvider([_answer()])
    result = reason([], [_prediction("p1")], SNAPSHOT, provider, now=NOW)
    assert result.reasoning is None and provider.prompts == []


def test_expired_inputs_are_left_out() -> None:
    old = _assessment("a0", until=timedelta(minutes=1))
    provider = FakeProvider([_answer(cited_assessment_ids=["a1"])])
    reason([old, _assessment("a1")], [], None, provider, now=NOW)
    assert "a0" not in provider.prompts[0]


def test_invented_citations_are_rejected() -> None:
    bad = _answer(cited_prediction_ids=["p404"])
    result = reason(
        [_assessment("a1")], [_prediction("p1")], None, FakeProvider([bad, bad]), now=NOW
    )
    assert result.reasoning is None and result.health.status is HealthStatus.DEGRADED
    assert "not given" in (result.health.detail or "")


def test_provider_failure_degrades_without_blocking() -> None:
    slow = ProviderTimeout("slow")
    result = reason([_assessment("a1")], [], None, FakeProvider([slow, slow]), now=NOW)
    assert result.reasoning is None and result.health.status is HealthStatus.DEGRADED


def test_no_provider_is_down() -> None:
    result = reason([_assessment("a1")], [], None, None, now=NOW)
    assert result.reasoning is None and result.health.status is HealthStatus.DOWN


def test_a_stale_snapshot_is_not_used() -> None:
    stale = SNAPSHOT.model_copy(update={"quality": DataQuality.STALE})
    provider = FakeProvider([_answer(cited_prediction_ids=[])])
    result = reason([_assessment("a1")], [], stale, provider, now=NOW)
    assert "H1.direction_10.direction" not in provider.prompts[0]
    assert result.reasoning is not None


# --- review findings ------------------------------------------------------------------------------


def test_a_snapshot_not_yet_available_is_not_used() -> None:
    future = SNAPSHOT.model_copy(update={"available_at": NOW + timedelta(minutes=2)})
    provider = FakeProvider([_answer(cited_prediction_ids=[])])
    result = reason([_assessment("a1")], [], future, provider, now=NOW)
    assert "H1.direction_10.direction" not in provider.prompts[0]
    assert result.reasoning is not None and result.reasoning.feature_snapshot_ref is None


def test_a_snapshot_of_another_schema_is_not_used() -> None:
    other = SNAPSHOT.model_copy(update={"feature_schema": "m02.features.v2"})
    provider = FakeProvider([_answer(cited_prediction_ids=[])])
    reason([_assessment("a1")], [], other, provider, now=NOW)
    assert "mtf.aligned" not in provider.prompts[0]


def test_forecasts_on_stale_data_are_left_out() -> None:
    stale = _prediction("p1").model_copy(update={"data_quality": DataQuality.STALE})
    provider = FakeProvider([_answer(cited_prediction_ids=[])])
    reason([_assessment("a1")], [stale], None, provider, now=NOW)
    assert "p1" not in provider.prompts[0]


def test_single_candle_direction_is_not_a_chart_summary() -> None:
    provider = FakeProvider([_answer()])
    reason([_assessment("a1")], [_prediction("p1")], SNAPSHOT, provider, now=NOW)
    assert "bar_shape" not in provider.prompts[0]


def test_duplicate_citations_are_collapsed_and_the_snapshot_is_recorded() -> None:
    answer = _answer(cited_assessment_ids=["a1", "a1"], cited_prediction_ids=["p1", "p1"])
    r = reason([_assessment("a1")], [_prediction("p1")], SNAPSHOT, FakeProvider([answer]), now=NOW)
    assert r.reasoning is not None
    assert r.reasoning.news_assessment_ids == ["a1"] and r.reasoning.prediction_ids == ["p1"]
    assert r.reasoning.feature_snapshot_ref == f"m02.features.v1@{T0.isoformat()}"


def test_invented_ids_never_reach_health_detail() -> None:
    bad = _answer(cited_prediction_ids=["<" + "x" * 5000 + ">"])
    result = reason(
        [_assessment("a1")], [_prediction("p1")], None, FakeProvider([bad, bad]), now=NOW
    )
    assert "xxxx" not in (result.health.detail or "")


class _Boom:
    provider_id = "real"
    model = "m"

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        raise ConnectionResetError("socket closed by https://api.example/?key=sk-secret")


def test_any_adapter_exception_degrades_instead_of_crashing() -> None:
    result = reason([_assessment("a1")], [], None, _Boom(), now=NOW)
    assert result.reasoning is None and result.health.status is HealthStatus.DEGRADED
    assert "sk-secret" not in (result.health.detail or "")


def test_reasoning_is_stamped_when_the_answer_returned() -> None:
    """#83 review: a slow call is not stamped available before it existed."""
    later = NOW + timedelta(seconds=20)
    result = reason([_assessment("a1")], [_prediction("p1")], SNAPSHOT, FakeProvider([_answer()]),
                    now=NOW, clock=lambda: later)  # fmt: skip
    assert result.reasoning is not None and result.reasoning.available_at == later


def test_evidence_that_expires_during_the_call_gives_no_reasoning() -> None:
    too_late = T0 + timedelta(hours=5)  # after every expiry it relied on
    result = reason([_assessment("a1")], [_prediction("p1")], SNAPSHOT, FakeProvider([_answer()]),
                    now=NOW, clock=lambda: too_late)  # fmt: skip
    assert result.reasoning is None and result.health.status is HealthStatus.DEGRADED


def test_the_prompt_separates_citable_ids_from_chart_context() -> None:
    """Models cited chart feature keys as ids, which invalidated whole answers (#99):
    the prompt names the citable ids and marks the chart summary as context only."""
    provider = FakeProvider([_answer()])
    reason([_assessment("a1")], [_prediction("p1")], SNAPSHOT, provider, now=NOW)
    prompt = provider.prompts[0]
    assert REASONING_PROMPT_VERSION == "m03.reason.v2"
    assert "Citable assessment ids: a1" in prompt
    assert "Citable prediction ids: p1" in prompt
    context = prompt.index("never cite")
    assert prompt.index("H1.direction_10.direction") > context  # chart keys only as context
    assert prompt.index("Citable assessment ids") < context


def test_with_no_forecast_the_prompt_says_there_is_none_to_cite() -> None:
    provider = FakeProvider([_answer(cited_prediction_ids=[])])
    reason([_assessment("a1")], [], SNAPSHOT, provider, now=NOW)
    assert "Citable prediction ids: none" in provider.prompts[0]
