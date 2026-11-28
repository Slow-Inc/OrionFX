"""EV and the triple-barrier label price the same event (#129 amendment; research backlog
item 0). The tb.v2 label is one bid-path event for both sides: UP (+d), DOWN (-d) or FLAT
(its drift, taken as 0), with no cost inside. `expected_value` for side s is the literal
size x (d x (p_s - p_opposite) - (spread + 2 x slippage)) - commission: every cost once,
on every outcome. So EV at each certain outcome equals what that outcome pays the side,
after the same costs, and it breaks even where the edge meets `cost_over_atr`."""

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.support.decision import COSTS, CYCLE, T0, D, _ctx, _forecast, _proposal

from app.core.bet import LABEL_VERSION, SLIPPAGE, STRESSED_COST, UnitCost, cost_over_atr
from app.core.contracts import CandleClosed, ClassProbabilities
from app.core.enums import Direction, Timeframe
from app.decision.fusion import CENT, TARGET_ATR, Evidence, expected_value, gather
from app.learning.bars import Horizon
from app.learning.labels import Outcome, TripleBarrierLabel, triple_barrier
from app.market_data.features import ATR_M15

M5 = timedelta(minutes=5)
CTX = _ctx()  # bid 2000, spread 0.20, ATR_M15 4, 0.01 lot of 100 oz
ATR = D(4)
START = T0 - M5  # the entry bar closes at the snapshot's as_of
SIDES = [Direction.UP, Direction.DOWN]
assert CTX.spread is not None
COST = UnitCost(spread=CTX.spread, slippage=SLIPPAGE,
                commission=2 * COSTS.commission_per_lot / COSTS.contract_size)  # fmt: skip


def _bar(i: int, *, high: str = "2000", low: str = "2000") -> CandleClosed:
    t = START + i * M5
    return CandleClosed(
        symbol="XAUUSD", timeframe=Timeframe.M5, open_time=t, close_time=t + M5,
        open=D(2000), high=D(high), low=D(low), close=D(2000), volume=None, spread=None,
        available_at=t + M5,
    )  # fmt: skip


def _label(first: CandleClosed) -> TripleBarrierLabel:
    """Twelve M5 bars, the forecast's H1 span, after the entry bar."""
    f = _forecast()
    n = Horizon(f.horizon, f.horizon_bars).length // M5
    assert CTX.value(ATR_M15) == ATR
    lab = triple_barrier(_bar(0), [first, *(_bar(i) for i in range(2, n + 1))], ATR, time_bars=n)
    assert lab is not None and lab.move is not None
    return lab


UP_PATH, DOWN_PATH, FLAT_PATH = _bar(1, high="2040"), _bar(1, low="1960"), _bar(1)


def _ev(direction: Direction, up: str, down: str, flat: str,
        label_version: str = LABEL_VERSION) -> Decimal | None:  # fmt: skip
    f = _forecast(direction=direction).model_copy(update={
        "class_probabilities": ClassProbabilities(label_version=label_version, up=D(up),
                                                  down=D(down), flat=D(flat))})  # fmt: skip
    inp = gather(CTX, [_proposal(direction=direction)], Evidence(forecast=f), cycle=CYCLE,
                 costs=COSTS, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    return expected_value(CTX, inp, COSTS)[0]


def _paid(direction: Direction, lab: TripleBarrierLabel) -> Decimal:
    """What the label's outcome pays `direction`, in USD for the initial lot, after
    the spread and two slippages per unit and the round-trip commission."""
    assert lab.move is not None
    sign = 1 if direction is Direction.UP else -1
    size = CTX.initial_lot * COSTS.contract_size
    return (size * (sign * lab.move - COST.spread - 2 * COST.slippage)
            - size * COST.commission).quantize(CENT)  # fmt: skip


@pytest.mark.parametrize("direction", SIDES)
def test_each_certain_outcome_is_priced_at_what_the_label_pays_the_side(
    direction: Direction,
) -> None:
    up, down, flat = _label(UP_PATH), _label(DOWN_PATH), _label(FLAT_PATH)
    assert (up.outcome, down.outcome, flat.outcome) == (Outcome.UP, Outcome.DOWN, Outcome.FLAT)
    assert flat.move == 0
    assert _ev(direction, "1", "0", "0") == _paid(direction, up)
    assert _ev(direction, "0", "1", "0") == _paid(direction, down)
    assert _ev(direction, "0", "0", "1") == _paid(direction, flat)
    # linear in between: the payoffs weighted by the same triple for either side
    weighted = (D("0.5") * up.move + D("0.2") * down.move) * (1 if direction is Direction.UP
                                                               else -1)  # fmt: skip
    size = CTX.initial_lot * COSTS.contract_size
    expected = size * (weighted - COST.fills) - size * COST.commission
    assert _ev(direction, "0.5", "0.2", "0.3") == expected.quantize(CENT)


def test_one_triple_prices_both_sides_as_mirror_images() -> None:
    long, short = _ev(Direction.UP, "0.6", "0.1", "0.3"), _ev(Direction.DOWN, "0.1", "0.6", "0.3")
    assert long == short == (D(4) * D("0.5") - D("0.30") - D("0.07")).quantize(CENT)


def test_the_barrier_is_the_distance_ev_prices() -> None:
    lab = _label(UP_PATH)
    assert lab.distance == TARGET_ATR * ATR == lab.move == -_label(DOWN_PATH).move  # type: ignore[operator]


def test_without_class_probabilities_of_this_label_ev_is_refused() -> None:
    inp = gather(CTX, [_proposal()], Evidence(forecast=_forecast(classes=False)), cycle=CYCLE,
                 costs=COSTS, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    assert expected_value(CTX, inp, COSTS) == (None, None)
    assert _ev(Direction.UP, "0.6", "0.1", "0.3", label_version="m07.labels.tb.v1") is None


@pytest.mark.parametrize("flat", ["0", "0.3", "0.9"])
def test_ev_breaks_even_where_the_edge_meets_cost_over_atr(flat: str) -> None:
    hurdle = cost_over_atr(COST, ATR)
    rest = 1 - D(flat)
    up, down = (rest + hurdle) / 2, (rest - hurdle) / 2
    assert _ev(Direction.UP, str(up), str(down), flat) == 0  # whatever the FLAT mass


def test_the_stressed_cost_is_pre_declared_and_dearer_than_the_live_one() -> None:
    assert (STRESSED_COST.spread, STRESSED_COST.slippage, STRESSED_COST.commission) == (
        D("0.80"), D("0.10"), D("0.07"))  # fmt: skip
    assert cost_over_atr(STRESSED_COST, ATR) > cost_over_atr(COST, ATR)
