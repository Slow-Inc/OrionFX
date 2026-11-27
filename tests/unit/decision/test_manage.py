"""M05 slice C: exits, scaling and recovery proposals, decision trace and replay (#54;
DEC-05/06/07, UST-12; AT-14, AT-41, AT-42). Exits are proposable whenever the evidence
turns; adding is an opportunity with positive EV, never forced, and never on a losing
cycle in Live."""

from dataclasses import replace
from decimal import Decimal

from tests.support.decision import (
    COSTS,
    CYCLE,
    NOW,
    _ctx,
    _forecast,
    _news,
    _proposal,
    _reasoning,
)

from app.core.enums import DecisionAction, Direction, IntentAction, Mode, Side, Timeframe
from app.decision.decide import decide
from app.decision.fusion import Evidence
from app.decision.manage import Held, manage
from app.decision.trace import DecisionTrace, replay

D = Decimal
HELD = Held(side=Side.BUY, positions=1, cycle_net_usd=D("2.50"),
            position_refs=["pos-1"])  # fmt: skip
OPEN = replace(CYCLE, open_positions=1)


def _manage(proposals=None, evidence=None, held=HELD, ctx=None, max_positions=3):  # type: ignore[no-untyped-def]
    return manage(
        ctx or _ctx(),
        [_proposal()] if proposals is None else proposals,
        evidence or Evidence(forecast=_forecast()),
        cycle=OPEN, held=held, max_positions=max_positions, costs=COSTS, symbol="XAUUSD",
        policy_version="p1",
    )  # fmt: skip


def _reasons(d) -> list[str]:  # type: ignore[no-untyped-def]
    return d.created.reason_codes  # type: ignore[no-any-return]


# --- exits ---------------------------------------------------------------------------------------


def test_reversed_evidence_proposes_closing_the_whole_basket() -> None:
    down = Direction.DOWN
    ev = Evidence(forecast=_forecast(direction=down), news=[_news(down)])
    d = _manage([_proposal(direction=down)], ev)
    assert d.created.action is DecisionAction.EXIT and "EVIDENCE_REVERSED" in _reasons(d)
    assert d.intent is not None and d.intent.action is IntentAction.CLOSE_ALL
    assert d.intent.side is None and d.intent.requested_lot is None
    assert d.intent.position_refs == ["pos-1"] and d.intent.cycle_id == "cyc_1"


def test_exit_needs_more_opposing_than_supporting_votes() -> None:
    # held BUY; forecast DOWN vs news UP: 1 vs 1 -> hold, not exit
    ev = Evidence(forecast=_forecast(direction=Direction.DOWN), news=[_news(Direction.UP)])
    d = _manage([], ev)
    assert d.created.action is DecisionAction.WAIT


def test_exit_ignores_entry_only_gates() -> None:
    # a losing cycle, a cap reached and no cost data do not block an exit
    down = Direction.DOWN
    ev = Evidence(forecast=_forecast(direction=down), news=[_news(down)])
    losing = replace(HELD, cycle_net_usd=D("-40"), positions=3)
    d = manage(_ctx(spread=None, mode=Mode.LIVE), [], ev, cycle=OPEN, held=losing,
               max_positions=3, costs=None, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    assert d.created.action is DecisionAction.EXIT


# --- adding / recovery ---------------------------------------------------------------------------


def test_adding_with_the_trend_uses_the_initial_lot_with_positive_ev() -> None:
    d = _manage()
    assert d.created.action is DecisionAction.ADD and "OPPORTUNITY" in _reasons(d)
    assert d.intent is not None and d.intent.action is IntentAction.ADD
    assert d.intent.requested_lot == D("0.01") and d.intent.side is Side.BUY


def test_no_add_beyond_the_position_cap_or_without_positive_ev() -> None:
    assert "CAP_REACHED" in _reasons(_manage(held=replace(HELD, positions=3)))
    assert "EV_NOT_POSITIVE" in _reasons(_manage(evidence=Evidence(forecast=_forecast(p="0.53"))))
    assert "EV_UNKNOWN" in _reasons(_manage(evidence=Evidence()))


def test_recovery_on_a_losing_cycle_is_an_opportunity_outside_live_only() -> None:
    losing = replace(HELD, cycle_net_usd=D("-5"))
    demo = _manage(held=losing)
    assert demo.created.action is DecisionAction.ADD and "RECOVERY_OPPORTUNITY" in _reasons(demo)
    assert demo.intent is not None and demo.intent.requested_lot == D("0.01")  # never sized up
    live = _manage(held=losing, ctx=_ctx(mode=Mode.LIVE))
    assert live.created.action is DecisionAction.WAIT and "NO_LIVE_RECOVERY" in _reasons(live)


def test_unknown_cycle_net_fails_closed_for_adding() -> None:
    d = _manage(held=replace(HELD, cycle_net_usd=None))
    assert "CYCLE_NET_UNKNOWN" in _reasons(d)


def test_opposite_proposal_without_enough_evidence_holds() -> None:
    d = _manage([_proposal(direction=Direction.DOWN)], Evidence(forecast=_forecast()))
    assert d.created.action is DecisionAction.WAIT and "HOLD" in _reasons(d)


def test_strategy_switch_keeps_the_cycle() -> None:
    d = _manage([_proposal("breakout")])
    assert d.intent is not None and d.intent.cycle_id == "cyc_1"
    assert d.created.strategy_id == "breakout"  # the cycle's target/DD live in M06, untouched


def test_stale_snapshot_waits_even_when_holding() -> None:
    late = _manage(ctx=_ctx(now=NOW.replace(minute=30)))
    assert "STALE_INPUT" in _reasons(late)


# --- trace and replay ----------------------------------------------------------------------------


def test_an_entry_decision_replays_from_its_stored_trace() -> None:
    args = {"cycle": CYCLE, "costs": COSTS, "symbol": "XAUUSD", "policy_version": "p1"}
    weak = Evidence(forecast=_forecast(), news=[_news(impact="0.5")],
                    reasoning=_reasoning(Direction.UP, []))  # fmt: skip
    strong = Evidence(forecast=_forecast(), news=[_news(impact="0.9")])  # a #115 veto
    for ev, action in ((weak, DecisionAction.ENTER), (strong, DecisionAction.WAIT)):
        original = decide(_ctx(), [_proposal()], ev, **args)  # type: ignore[arg-type]
        assert original.created.action is action
        trace = DecisionTrace.capture(_ctx(), [_proposal()], ev, **args)  # type: ignore[arg-type]
        stored = trace.model_dump_json()
        again = replay(DecisionTrace.model_validate_json(stored))
        assert again.created == original.created and again.intent == original.intent


def test_a_managed_decision_replays_too() -> None:
    original = _manage()
    trace = DecisionTrace.capture(
        _ctx(), [_proposal()], Evidence(forecast=_forecast()), cycle=OPEN, costs=COSTS,
        symbol="XAUUSD", policy_version="p1", held=HELD, max_positions=3,
    )  # fmt: skip
    again = replay(DecisionTrace.model_validate_json(trace.model_dump_json()))
    assert again.created == original.created and again.intent == original.intent


def test_add_rules_mirror_entry_rules() -> None:
    break_even = _manage(evidence=Evidence(forecast=_forecast(p="0.53375")))
    assert "EV_NOT_POSITIVE" in _reasons(break_even)
    # chart agrees with the position but a forecast opposes it: 1 vs 1 -> no exit, no add
    split = _manage(evidence=Evidence(forecast=_forecast(direction=Direction.DOWN)))
    assert "CONFLICT_EVIDENCE" in _reasons(split)
    pending = replace(OPEN, pending=frozenset({IntentAction.ADD}))
    again = manage(_ctx(), [_proposal()], Evidence(forecast=_forecast()), cycle=pending,
                   held=HELD, max_positions=3, costs=COSTS, symbol="XAUUSD",
                   policy_version="p1")  # fmt: skip
    assert "DUPLICATE" in _reasons(again) and again.intent is None


# --- review fixes ---


def test_no_second_exit_while_one_is_pending() -> None:
    down = Direction.DOWN
    ev = Evidence(forecast=_forecast(direction=down), news=[_news(down)])
    pending = replace(OPEN, pending=frozenset({IntentAction.CLOSE_ALL}))
    d = manage(_ctx(), [], ev, cycle=pending, held=HELD, max_positions=3, costs=COSTS,
               symbol="XAUUSD", policy_version="p1")  # fmt: skip
    assert "EXIT_PENDING" in _reasons(d) and d.intent is None


def test_a_floating_cycle_net_does_not_change_the_decision_id() -> None:
    a = _manage()
    b = _manage(held=replace(HELD, cycle_net_usd=D("2.47")))
    assert a.created.decision_id == b.created.decision_id


def test_live_evidence_can_still_exit_on_a_stale_snapshot() -> None:
    down = Direction.DOWN
    ev = Evidence(forecast=_forecast(direction=down), news=[_news(down)])
    stale = _ctx(now=NOW.replace(minute=30))
    fresh_forecast = ev.forecast.model_copy(update={"valid_until": NOW.replace(hour=13)})  # type: ignore[union-attr]
    fresh_news = ev.news[0].model_copy(update={"valid_until": NOW.replace(hour=13)})
    live_ev = Evidence(forecast=fresh_forecast, news=[fresh_news])
    assert _manage([_proposal()], live_ev, ctx=stale).created.action is DecisionAction.EXIT
    # forecast DOWN alone vs a stale chart UP: the chart is dropped, so 1 vs 0 still exits
    live_up = _proposal().model_copy(update={"valid_until": NOW.replace(hour=13)})
    d = _manage([live_up], Evidence(forecast=fresh_forecast), ctx=stale)
    assert d.created.action is DecisionAction.EXIT


def test_trace_needs_held_and_cap_together_and_accepts_a_zero_cap() -> None:
    import pytest

    args = {"cycle": OPEN, "costs": COSTS, "symbol": "XAUUSD", "policy_version": "p1"}
    with pytest.raises(ValueError):
        DecisionTrace.capture(_ctx(), [], Evidence(), held=HELD, **args)  # type: ignore[arg-type]
    t = DecisionTrace.capture(_ctx(), [_proposal()], Evidence(forecast=_forecast()), held=HELD,
                              max_positions=0, **args)  # type: ignore[arg-type]  # fmt: skip
    assert "CAP_REACHED" in _reasons(replay(DecisionTrace.model_validate_json(t.model_dump_json())))


def test_the_trace_captures_every_input_field() -> None:
    from dataclasses import fields

    from app.decision.context import DecisionContext
    from app.decision.fusion import Costs, CycleView
    from app.decision.trace import ContextTrace, CostsTrace, CycleTrace, EvidenceTrace, HeldTrace

    pairs = [(DecisionContext, ContextTrace), (Evidence, EvidenceTrace), (CycleView, CycleTrace),
             (Costs, CostsTrace), (Held, HeldTrace)]  # fmt: skip
    for source, trace in pairs:
        assert {f.name for f in fields(source)} == set(trace.model_fields), source


def test_a_managed_decision_bets_on_the_held_side_and_an_exit_against_it() -> None:
    """#90: what M07 scores. ADD/WAIT lean to the held basket; an EXIT bets against it;
    a horizon is never borrowed from a proposal on the other side."""
    down = Direction.DOWN
    exit_ = _manage([_proposal(direction=down)],
                    Evidence(forecast=_forecast(direction=down), news=[_news(down)]))  # fmt: skip
    hold = _manage([_proposal(direction=down)])
    add = _manage()
    assert exit_.created.action is DecisionAction.EXIT and exit_.direction is down
    assert exit_.horizon is Timeframe.H1  # the DOWN proposal's horizon
    assert hold.created.reason_codes == ["HOLD"] and hold.direction is Direction.UP
    assert hold.horizon is None  # only a DOWN proposal: no UP horizon to borrow
    assert add.created.action is DecisionAction.ADD and add.direction is Direction.UP
    assert add.horizon is Timeframe.H1


def test_the_news_veto_blocks_an_add_but_never_an_exit() -> None:
    """#121 (MAJ-05.1, owner decision): the #115 veto also guards ADD. Forecast and chart
    UP against strong DOWN news is 2 vs 1, which would add; the veto turns it into WAIT.
    Strong news against a held BUY still counts as one exit vote exactly as before."""
    strong = Evidence(forecast=_forecast(), news=[_news(impact="1")])
    d = _manage(evidence=strong)
    # its own code, so the #116 entry-veto report is not mixed with ADD vetoes
    assert d.created.action is DecisionAction.WAIT and _reasons(d) == ["NEWS_VETO_ADD"]
    assert d.intent is None
    weak = _manage(evidence=Evidence(forecast=_forecast(), news=[_news(impact="0.5")]))
    assert weak.created.action is DecisionAction.ADD  # below the threshold: no veto
    alone = _manage([], Evidence(news=[_news(impact="1")]))
    assert alone.created.action is DecisionAction.EXIT  # the news still calls the exit


def test_an_exit_on_a_stale_snapshot_is_not_born_expired() -> None:
    """#121: the snapshot's validity must not clamp an EXIT intent, or the loop drops it
    from `pending` at once and re-proposes CLOSE_ALL every tick."""
    from app.decision.fusion import INTENT_TTL

    later = NOW.replace(hour=13)
    down = Direction.DOWN
    ev = Evidence(forecast=_forecast(direction=down, valid_until=later),
                  news=[_news(down, valid_until=later)])  # fmt: skip
    stale = _ctx(now=NOW.replace(minute=30))
    d = _manage([], ev, ctx=stale)
    assert d.created.action is DecisionAction.EXIT and d.intent is not None
    assert d.intent.valid_until == stale.now + INTENT_TTL


def test_open_and_add_intents_still_expire_with_the_snapshot() -> None:
    """#121: only exits escape the snapshot's expiry; an OPEN or ADD rests on it."""
    from tests.support.decision import LATER

    from app.decision.fusion import INTENT_TTL

    near_end = _ctx(now=LATER - INTENT_TTL / 3)
    lasting = _proposal().model_copy(update={"valid_until": LATER + INTENT_TTL * 10})
    add = _manage([lasting], ctx=near_end)
    enter = decide(near_end, [lasting], Evidence(forecast=_forecast()), cycle=CYCLE,
                   costs=COSTS, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    for d, action in ((add, IntentAction.ADD), (enter, IntentAction.OPEN)):
        assert d.intent is not None and d.intent.action is action
        assert d.intent.valid_until == LATER < near_end.now + INTENT_TTL


def test_the_horizon_falls_back_when_the_chosen_proposal_is_on_the_other_side() -> None:
    """#121: the forecast picks the DOWN H1 proposal, but a held BUY leans UP, so the
    horizon comes from the UP proposal (M15), never from the opposing chosen one."""
    down_h1 = _proposal("trend_following", Direction.DOWN)
    up_m15 = _proposal("breakout").model_copy(update={"horizon": Timeframe.M15})
    ev = Evidence(forecast=_forecast(direction=Direction.DOWN), news=[_news(Direction.UP)])
    d = _manage([down_h1, up_m15], ev)
    assert d.created.strategy_id == "trend_following" and d.direction is Direction.UP
    assert d.created.action is DecisionAction.WAIT and d.horizon is Timeframe.M15
