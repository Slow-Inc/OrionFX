"""M05 slice B: fusion, EV after costs, WAIT rules, decisions and trade intents (#53;
DEC-01/02/04/05, UST-03; AT-14, AT-18, AT-19, AT-34). `decide` is pure: same inputs, same
decision; WAIT whenever evidence is missing, stale, conflicting or EV is not positive."""

from dataclasses import replace
from datetime import timedelta

from tests.support.decision import (
    COSTS,
    CYCLE,
    LATER,
    NOW,
    D,
    _ctx,
    _forecast,
    _news,
    _proposal,
    _reasoning,
)

from app.core.contracts import (
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
from app.decision.decide import decide
from app.decision.fusion import Costs, CycleView, Evidence, to_events


def _decide(proposals: list[StrategyProposal] | None = None, evidence: Evidence | None = None,
            ctx: DecisionContext | None = None, cycle: CycleView = CYCLE,
            costs: Costs | None = COSTS):  # type: ignore[no-untyped-def]  # fmt: skip
    return decide(
        ctx or _ctx(),
        [_proposal()] if proposals is None else proposals,
        evidence or Evidence(forecast=_forecast()),
        cycle=cycle, costs=costs, symbol="XAUUSD", policy_version="p1",
    )  # fmt: skip


def _weak(**over: object):  # type: ignore[no-untyped-def]
    """Opposing news below the #115 veto threshold: for tests about vote counting."""
    return _news(impact="0.5", **over)  # type: ignore[arg-type]


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
    assert d.created.action is DecisionAction.WAIT and "CONFLICT_STRATEGIES" in _reasons(d)


def test_opposing_forecast_waits() -> None:
    d = _decide(evidence=Evidence(forecast=_forecast(direction=Direction.DOWN)))
    assert d.created.action is DecisionAction.WAIT and "CONFLICT_EVIDENCE" in _reasons(d)


def test_opposing_cited_news_waits_but_uncited_news_is_ignored() -> None:
    # weak news (impact 0.5, below the #115 veto): this test is about vote counting
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[_weak(cited=True)]))
    assert d.created.action is DecisionAction.ENTER  # 2 agree (chart, forecast) vs 1 news
    both = Evidence(forecast=_forecast(direction=Direction.DOWN), news=[_weak()])
    assert "CONFLICT_EVIDENCE" in _reasons(_decide(evidence=both))
    uncited = _decide(evidence=Evidence(forecast=_forecast(), news=[_weak(cited=False)]))
    assert "na_1" not in uncited.created.evidence_refs


def test_reasoning_that_restates_counted_news_is_not_a_second_vote() -> None:
    # weak news (impact 0.5, below the #115 veto): this test is about vote counting
    # news DOWN + reasoning DOWN citing that same news: one opposing vote, not two
    ev = Evidence(
        forecast=_forecast(),
        news=[_weak()],
        reasoning=_reasoning(Direction.DOWN, ["na_1"]),
    )
    assert _decide(evidence=ev).created.action is DecisionAction.ENTER
    independent = replace(ev, reasoning=_reasoning(Direction.DOWN, []))
    assert "CONFLICT_EVIDENCE" in _reasons(_decide(evidence=independent))


def test_news_inside_a_news_enhanced_forecast_is_not_counted_again() -> None:
    # weak news (impact 0.5, below the #115 veto): this test is about vote counting
    # news-enhanced forecast UP + news DOWN: the news is inside the forecast, so 2 votes vs 0
    enhanced_up = _forecast(lineage=ForecastLineage.NEWS_ENHANCED)
    d = _decide(evidence=Evidence(forecast=enhanced_up, news=[_weak()]))
    assert (
        d.created.action is DecisionAction.ENTER
        and d.created.reason_codes[-1] == "VOTES_2_AGAINST_0"
    )
    chart_only = _decide(evidence=Evidence(forecast=_forecast(), news=[_weak()]))
    assert chart_only.created.action is DecisionAction.ENTER
    # plus an independent reasoning DOWN: news-enhanced 2 vs 1 enters; chart-only 2 vs 2 waits
    other = _reasoning(Direction.DOWN, [])
    ev = Evidence(forecast=enhanced_up, news=[_weak()], reasoning=other)
    assert _decide(evidence=ev).created.action is DecisionAction.ENTER
    assert "CONFLICT_EVIDENCE" in _reasons(_decide(evidence=replace(ev, forecast=_forecast())))


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
    again = _decide(cycle=replace(CYCLE, pending=frozenset({IntentAction.OPEN})))
    assert "DUPLICATE" in _reasons(again) and again.intent is None
    other_cycle = _decide(cycle=replace(CYCLE, cycle_id="cyc_2"))
    assert other_cycle.created.decision_id != a.created.decision_id


def test_events_are_valid_deterministic_and_causally_linked() -> None:
    d = _decide()
    events = to_events(d, account_id="acc", bot_id="bot")
    assert [e.event_type for e in events] == ["decision.created", "trade.intent"]
    assert events[1].causation_id == events[0].event_id
    assert events[0].correlation_id == events[1].correlation_id == d.created.decision_id
    again = to_events(d, account_id="acc", bot_id="bot")
    assert [e.event_id for e in again] == [e.event_id for e in events]
    assert all(e.mode is Mode.DEMO and e.cycle_id == "cyc_1" for e in events)
    for e in events:
        parse_event(e.model_dump(mode="json"))
    wait = to_events(_decide(proposals=[]), account_id="a", bot_id="b")
    assert [e.event_type for e in wait] == ["decision.created"]
    assert wait[0].cycle_id == "cyc_1"  # a WAIT inside a cycle keeps its cycle link


# --- review fixes -----------------------------------------------------------------------------


def test_stale_snapshot_or_expired_proposal_never_enters() -> None:
    late = _decide(ctx=_ctx(now=LATER))
    assert late.created.action is DecisionAction.WAIT and "STALE_INPUT" in _reasons(late)
    expired = _proposal().model_copy(update={"valid_until": NOW})
    assert "NO_OPPORTUNITY" in _reasons(_decide([expired]))


def test_neutral_or_non_open_proposals_are_not_entries() -> None:
    # the contract already forbids a NEUTRAL OPEN; a NEUTRAL CLOSE is legal but not an entry
    closing = _proposal().model_copy(update={"action": IntentAction.CLOSE})
    neutral_close = closing.model_copy(update={"direction": Direction.NEUTRAL})
    for p in (closing, neutral_close):
        assert "NO_OPPORTUNITY" in _reasons(_decide([p]))


def test_non_positive_initial_lot_waits_instead_of_raising() -> None:
    assert "INVALID_LOT" in _reasons(_decide(ctx=_ctx(initial_lot=D(0))))


def test_reasoning_restating_the_forecast_is_not_a_second_vote() -> None:
    # weak news (impact 0.5, below the #115 veto): this test is about vote counting
    # proposal UP, forecast UP, news DOWN: 2 vs 1. A reasoning UP that cites pred_1 adds nothing.
    restating = _reasoning(Direction.UP, []).model_copy(update={"prediction_ids": ["pred_1"]})
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[_weak()], reasoning=restating))
    assert d.created.reason_codes[-1] == "VOTES_2_AGAINST_1"


def test_reasoning_built_on_stale_news_is_ignored() -> None:
    old = _news().model_copy(update={"assessment_id": "na_old", "valid_until": NOW})
    on_old = _reasoning(Direction.DOWN, ["na_old"])
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[old], reasoning=on_old))
    assert d.created.action is DecisionAction.ENTER and "rs_1" not in d.created.evidence_refs


def test_reasoning_citing_news_that_cast_no_vote_still_votes() -> None:
    up = _news(Direction.UP).model_copy(update={"assessment_id": "na_up", "impact": D("0.5")})
    down = _news(Direction.DOWN).model_copy(update={"impact": D("0.5")})  # nets to zero
    r = _reasoning(Direction.DOWN, ["na_up"])
    ev = Evidence(forecast=_forecast(), news=[up, down], reasoning=r)
    assert _decide(evidence=ev).created.reason_codes[-1] == "VOTES_2_AGAINST_1"


def test_forecast_must_have_ok_data_and_the_trade_horizon_to_price_ev() -> None:
    degraded = _forecast().model_copy(update={"data_quality": DataQuality.DEGRADED})
    assert "EV_UNKNOWN" in _reasons(_decide(evidence=Evidence(forecast=degraded)))
    other_horizon = _forecast().model_copy(update={"horizon": Timeframe.M15})
    assert "EV_UNKNOWN" in _reasons(_decide(evidence=Evidence(forecast=other_horizon)))


def test_ev_is_only_reported_once_every_other_gate_passed() -> None:
    d = _decide(cycle=replace(CYCLE, open_positions=1))
    assert d.created.expected_value_usd is None


def test_a_different_lot_is_a_different_decision() -> None:
    a = _decide()
    b = _decide(ctx=_ctx(initial_lot=D("0.02")))
    assert a.created.decision_id != b.created.decision_id
    assert (
        a.intent is not None and b.intent is not None and a.intent.intent_id != b.intent.intent_id
    )


def test_costs_come_from_the_symbol_spec() -> None:
    from app.execution.broker import SymbolSpec

    spec = SymbolSpec("XAUUSD", D(100), D("0.01"), D("0.01"), D(100), D("0.01"), D(1000), True)
    assert Costs.from_spec(spec, D("3.5")) == COSTS


def test_a_forecast_with_unusable_data_neither_votes_nor_is_cited() -> None:
    bad = _forecast(direction=Direction.DOWN).model_copy(update={"data_quality": DataQuality.STALE})
    d = _decide(evidence=Evidence(forecast=bad))
    assert "EV_UNKNOWN" in _reasons(d)  # not CONFLICT_EVIDENCE: the stale forecast has no vote
    assert "pred_1" not in d.created.evidence_refs


def test_reasoning_produced_after_now_is_not_used() -> None:
    """#83: no look-ahead through LLM reasoning (AT-15)."""
    later = _reasoning(Direction.DOWN, []).model_copy(
        update={"available_at": NOW + timedelta(minutes=1)}
    )
    ev = Evidence(forecast=_forecast(), reasoning=later)
    d = _decide(evidence=ev)
    assert "rs_1" not in d.created.evidence_refs
    assert d.created.action is DecisionAction.ENTER  # the future DOWN vote did not count


def test_reasoning_available_exactly_at_now_is_used() -> None:
    # weak news (impact 0.5, below the #115 veto): this test is about vote counting
    at_now = _reasoning(Direction.DOWN, []).model_copy(update={"available_at": NOW})
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[_weak()], reasoning=at_now))
    assert "rs_1" in d.created.evidence_refs
    assert "CONFLICT_EVIDENCE" in d.created.reason_codes  # its DOWN vote counted: 2 vs 2


def test_a_decision_carries_the_direction_and_horizon_it_acted_on() -> None:
    """#90: M07 learns from what was decided, not from what the loop guesses."""
    d = _decide([_proposal(direction=Direction.UP)])
    assert d.direction is Direction.UP and d.horizon is Timeframe.H1
    nothing = _decide([])
    assert nothing.direction is None and nothing.horizon is None


def test_conflicting_strategies_lean_nowhere() -> None:
    d = _decide([_proposal("a", Direction.UP), _proposal("b", Direction.DOWN)])
    assert d.direction is None and d.horizon is None


# --- news veto (#115, PRD #114) ----------------------------------------------------------------


def test_strong_opposing_news_vetoes_an_entry_the_votes_would_allow() -> None:
    """Benchmark E2 shape: chart and forecast agree, news says the opposite."""
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[_news(impact="0.8")]))
    assert d.created.action is DecisionAction.WAIT and _reasons(d) == ["NEWS_VETO"]
    assert d.intent is None and "na_1" in d.created.evidence_refs
    assert d.direction is Direction.UP  # the side it refused, for the M07 counterfactual


def test_the_veto_needs_impact_at_the_threshold() -> None:
    at = _decide(evidence=Evidence(forecast=_forecast(), news=[_news(impact="0.6")]))
    below = _decide(evidence=Evidence(forecast=_forecast(), news=[_news(impact="0.59")]))
    assert _reasons(at) == ["NEWS_VETO"]
    assert below.created.action is DecisionAction.ENTER


def test_agreeing_neutral_uncited_or_expired_news_never_vetoes() -> None:
    for news in (
        _news(Direction.UP, impact="1"),
        _news(Direction.NEUTRAL, impact="1"),
        _news(cited=False, impact="1"),
        _news(impact="1", valid_until=NOW),
    ):
        d = _decide(evidence=Evidence(forecast=_forecast(), news=[news]))
        assert d.created.action is DecisionAction.ENTER, news


def test_opposing_items_net_out_before_the_veto() -> None:
    down = _news(impact="0.8", aid="na_down")
    up = _news(Direction.UP, impact="0.5", aid="na_up")
    d = _decide(evidence=Evidence(forecast=_forecast(), news=[down, up]))
    assert d.created.action is DecisionAction.ENTER  # net 0.3 against: below 0.6
    strong = _news(impact="1", aid="na_strong")
    d2 = _decide(evidence=Evidence(forecast=_forecast(), news=[down, strong, up]))
    assert _reasons(d2) == ["NEWS_VETO"]  # net 1.3 against


def test_a_news_enhanced_forecast_does_not_silence_the_veto() -> None:
    enhanced = _forecast(lineage=ForecastLineage.NEWS_ENHANCED)
    d = _decide(evidence=Evidence(forecast=enhanced, news=[_news(impact="0.9")]))
    assert _reasons(d) == ["NEWS_VETO"]


def test_no_news_means_the_veto_changes_nothing() -> None:
    assert _decide(evidence=Evidence(forecast=_forecast())).created.action is DecisionAction.ENTER


def test_the_veto_never_opens_a_trade() -> None:
    """Strong news with no chart proposal, or against an empty evidence set, stays WAIT for
    its usual reason; news alone never enters."""
    d = _decide(
        proposals=[],
        evidence=Evidence(forecast=_forecast(), news=[_news(Direction.UP, impact="1")]),
    )
    assert d.created.action is DecisionAction.WAIT and _reasons(d) == ["NO_OPPORTUNITY"]


def test_good_news_vetoes_a_sell_entry() -> None:
    down = _proposal(direction=Direction.DOWN)
    ev = Evidence(forecast=_forecast(direction=Direction.DOWN),
                  news=[_news(Direction.UP, impact="0.8")])  # fmt: skip
    d = _decide([down], ev)
    assert _reasons(d) == ["NEWS_VETO"] and d.direction is Direction.DOWN
    agreeing = Evidence(forecast=_forecast(direction=Direction.DOWN), news=[_news(impact="0.8")])
    assert _decide([down], agreeing).created.action is DecisionAction.ENTER


def test_the_proposal_whose_horizon_matches_the_forecast_is_chosen() -> None:
    """#126 (MAJ-04.3): with an M15 and an H1 proposal agreeing, an H1 forecast must price
    the H1 one; the alphabetically first (breakout, M15) gave EV_UNKNOWN."""
    m15 = _proposal("breakout").model_copy(update={"horizon": Timeframe.M15})
    d = _decide([m15, _proposal("trend_following")])
    assert d.created.action is DecisionAction.ENTER
    assert d.created.strategy_id == "trend_following"


def test_without_a_horizon_match_the_first_proposal_is_priced() -> None:
    m15 = _proposal("breakout").model_copy(update={"horizon": Timeframe.M15})
    d = _decide([m15], Evidence(forecast=_forecast()))
    assert d.created.strategy_id == "breakout"  # usable[0]; EV then needs the match


def test_conflicting_proposals_credit_the_one_agreeing_with_the_forecast() -> None:
    up, down = _proposal("trend_following"), _proposal("breakout", Direction.DOWN)
    d = _decide([down, up], Evidence(forecast=_forecast()))
    assert d.created.action is DecisionAction.WAIT and d.created.strategy_id == "trend_following"


def test_the_decision_horizon_is_the_chosen_proposals_horizon() -> None:
    """#121 (M1): the trade prices the H1 proposal, so replay and M07 must see H1, not the
    alphabetically first same-side proposal (breakout, M15)."""
    m15 = _proposal("breakout").model_copy(update={"horizon": Timeframe.M15})
    d = _decide([m15, _proposal("trend_following")])
    assert d.created.strategy_id == "trend_following" and d.horizon is Timeframe.H1
