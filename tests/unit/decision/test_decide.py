"""M05 slice B: fusion, EV after costs, WAIT rules, decisions and trade intents (#53;
DEC-01/02/04/05, UST-03; AT-14, AT-18, AT-19, AT-34). `decide` is pure: same inputs, same
decision; WAIT whenever evidence is missing, stale, conflicting or EV is not positive."""

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
from app.core.enums import (
    DataQuality,
    DecisionAction,
    Direction,
    ForecastLineage,
    IntentAction,
    Mode,
    Side,
    Timeframe,
)
from app.core.events import parse_event
from app.decision.context import DecisionContext
from app.decision.decide import Costs, CycleView, Evidence, decide, to_events
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


def _news(direction: Direction = Direction.DOWN, cited: bool = True) -> NewsAssessment:
    return NewsAssessment(
        assessment_id="na_1", news_ids=["n1"], source_refs=["src"] if cited else [],
        direction=direction, impact=D("0.8"),
        score=Confidence(value=D("0.6"), semantics="uncalibrated LLM score"),
        available_at=T0, valid_until=LATER,
    )  # fmt: skip


def _reasoning(direction: Direction, cites: list[str]) -> LlmReasoning:
    return LlmReasoning(
        reasoning_id="rs_1", news_assessment_ids=cites, prediction_ids=[], direction=direction,
        score=Confidence(value=D("0.6"), semantics="uncalibrated LLM score"), rationale="r",
        prompt_version="v", provider="fake", valid_until=LATER,
    )  # fmt: skip


COSTS = Costs(contract_size=D(100), commission_per_lot=D("3.5"))
CYCLE = CycleView(
    cycle_id="cyc_1", open_positions=0, last_closed_at=None, pending_decisions=frozenset()
)


def _decide(proposals: list[StrategyProposal] | None = None, evidence: Evidence | None = None,
            ctx: DecisionContext | None = None, cycle: CycleView = CYCLE,
            costs: Costs | None = COSTS):  # type: ignore[no-untyped-def]  # fmt: skip
    return decide(
        ctx or _ctx(),
        [_proposal()] if proposals is None else proposals,
        evidence or Evidence(forecast=_forecast()),
        cycle=cycle, costs=costs, symbol="XAUUSD", policy_version="p1",
    )  # fmt: skip


# --- ENTER -------------------------------------------------------------------------------------


def test_positive_ev_with_agreeing_evidence_enters_with_the_initial_lot() -> None:
    d = _decide()
    assert d.created.action is DecisionAction.ENTER and d.created.side is Side.BUY
    # move = 1.0 x ATR 4 = 4.00 USD/oz; 0.01 lot x 100 oz -> 4.00 USD at stake
    # EV = 4.00 x (2 x 0.70 - 1) - (0.20 x 1 + 3.5 x 0.01 x 2) = 1.60 - 0.27 = 1.33
    assert d.created.expected_value_usd == D("1.33")
    assert d.intent is not None
    assert d.intent.action is IntentAction.OPEN and d.intent.requested_lot == D("0.01")
    assert d.intent.decision_id == d.created.decision_id and d.intent.cycle_id == "cyc_1"
    assert d.intent.prediction_ids == ["pred_1"] and d.intent.mode is Mode.DEMO
    assert d.intent.valid_until <= NOW + timedelta(seconds=30)


def test_sell_side_follows_a_down_proposal() -> None:
    d = _decide(
        [_proposal(direction=Direction.DOWN)],
        Evidence(forecast=_forecast(direction=Direction.DOWN)),
    )
    assert d.created.side is Side.SELL and d.created.action is DecisionAction.ENTER


# --- WAIT rules --------------------------------------------------------------------------------


def _reasons(d: object) -> list[str]:
    return d.created.reason_codes  # type: ignore[attr-defined, no-any-return]


def test_no_proposal_waits() -> None:
    d = _decide(proposals=[])
    assert d.created.action is DecisionAction.WAIT and d.intent is None
    assert "NO_OPPORTUNITY" in _reasons(d)


def test_strategies_disagreeing_waits() -> None:
    d = _decide([_proposal("a"), _proposal("b", Direction.DOWN)])
    assert d.created.action is DecisionAction.WAIT and "CONFLICT" in _reasons(d)


def test_opposing_forecast_waits() -> None:
    d = _decide(evidence=Evidence(forecast=_forecast(direction=Direction.DOWN)))
    assert d.created.action is DecisionAction.WAIT and "CONFLICT" in _reasons(d)


def test_opposing_cited_news_waits_but_uncited_news_is_ignored() -> None:
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[_news(cited=True)]))
    assert d.created.action is DecisionAction.ENTER  # 2 agree (chart, forecast) vs 1 news
    both = Evidence(forecast=_forecast(direction=Direction.DOWN), news=[_news()])
    assert "CONFLICT" in _reasons(_decide(evidence=both))
    uncited = _decide(evidence=Evidence(forecast=_forecast(), news=[_news(cited=False)]))
    assert "na_1" not in uncited.created.evidence_refs


def test_reasoning_that_restates_counted_news_is_not_a_second_vote() -> None:
    # news DOWN + reasoning DOWN citing that same news: one opposing vote, not two
    ev = Evidence(
        forecast=_forecast(), news=[_news()], reasoning=_reasoning(Direction.DOWN, ["na_1"])
    )
    assert _decide(evidence=ev).created.action is DecisionAction.ENTER
    independent = replace(ev, reasoning=_reasoning(Direction.DOWN, []))
    assert "CONFLICT" in _reasons(_decide(evidence=independent))


def test_news_inside_a_news_enhanced_forecast_is_not_counted_again() -> None:
    # news-enhanced forecast UP + news DOWN: the news is inside the forecast, so 2 votes vs 0
    enhanced_up = _forecast(lineage=ForecastLineage.NEWS_ENHANCED)
    d = _decide(evidence=Evidence(forecast=enhanced_up, news=[_news()]))
    assert d.created.action is DecisionAction.ENTER and d.created.reason_codes[-1] == "VOTES_2"
    chart_only = _decide(evidence=Evidence(forecast=_forecast(), news=[_news()]))
    assert chart_only.created.action is DecisionAction.ENTER
    # plus an independent reasoning DOWN: news-enhanced 2 vs 1 enters; chart-only 2 vs 2 waits
    other = _reasoning(Direction.DOWN, [])
    ev = Evidence(forecast=enhanced_up, news=[_news()], reasoning=other)
    assert _decide(evidence=ev).created.action is DecisionAction.ENTER
    assert "CONFLICT" in _reasons(_decide(evidence=replace(ev, forecast=_forecast())))


def test_without_a_calibrated_probability_ev_is_unknown_and_waits() -> None:
    for ev in (Evidence(), Evidence(forecast=_forecast(calibrated=False))):
        d = _decide(evidence=ev)
        assert d.created.action is DecisionAction.WAIT and "EV_UNKNOWN" in _reasons(d)


def test_missing_cost_input_waits() -> None:
    assert "EV_UNKNOWN" in _reasons(_decide(costs=None))
    assert "EV_UNKNOWN" in _reasons(_decide(ctx=_ctx(spread=None)))
    assert "EV_UNKNOWN" in _reasons(
        _decide(ctx=_ctx(snapshot=_ctx().snapshot.model_copy(update={"values": {}})))
    )


def test_ev_not_above_costs_waits() -> None:
    d = _decide(evidence=Evidence(forecast=_forecast(p="0.53")))  # 4 x 0.06 = 0.24 < 0.27
    assert d.created.action is DecisionAction.WAIT and "EV_NOT_POSITIVE" in _reasons(d)
    assert d.created.expected_value_usd == D("-0.03")
    break_even = _decide(evidence=Evidence(forecast=_forecast(p="0.53375")))  # EV exactly 0
    assert "EV_NOT_POSITIVE" in _reasons(break_even)


def test_stale_forecast_is_ignored() -> None:
    d = _decide(evidence=Evidence(forecast=_forecast(valid_until=NOW)))
    assert "EV_UNKNOWN" in _reasons(d) and "pred_1" not in d.created.evidence_refs


def test_cooldown_after_a_closed_cycle_waits() -> None:
    d = _decide(cycle=replace(CYCLE, last_closed_at=NOW - timedelta(minutes=2)))
    assert "COOLDOWN" in _reasons(d)
    assert (
        _decide(cycle=replace(CYCLE, last_closed_at=NOW - timedelta(minutes=20))).intent is not None
    )


def test_open_position_means_no_fresh_entry() -> None:
    d = _decide(cycle=replace(CYCLE, open_positions=1))
    assert d.created.action is DecisionAction.WAIT and "POSITION_OPEN" in _reasons(d)


# --- idempotency and events ---------------------------------------------------------------------


def test_same_inputs_give_the_same_decision_and_a_repeat_waits() -> None:
    a, b = _decide(), _decide()
    assert a.created == b.created and a.intent == b.intent
    again = _decide(cycle=replace(CYCLE, pending_decisions=frozenset({a.created.decision_id})))
    assert "DUPLICATE" in _reasons(again) and again.intent is None
    other_cycle = _decide(cycle=replace(CYCLE, cycle_id="cyc_2"))
    assert other_cycle.created.decision_id != a.created.decision_id


def test_events_are_valid_deterministic_and_causally_linked() -> None:
    d = _decide()
    events = to_events(d, account_id="acc", bot_id="bot", mode=Mode.DEMO)
    assert [e.event_type for e in events] == ["decision.created", "trade.intent"]
    assert events[1].causation_id == events[0].event_id
    assert events[0].correlation_id == events[1].correlation_id == d.created.decision_id
    assert [e.event_id for e in to_events(d, account_id="acc", bot_id="bot", mode=Mode.DEMO)] == [
        e.event_id for e in events
    ]
    for e in events:
        parse_event(e.model_dump(mode="json"))
    assert [e.event_type for e in to_events(_decide(proposals=[]), account_id="a", bot_id="b",
                                            mode=Mode.DEMO)] == ["decision.created"]  # fmt: skip
