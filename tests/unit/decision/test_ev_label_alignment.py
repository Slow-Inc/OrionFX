"""EV and the triple-barrier label price the same event (#129 amendment; research backlog
item 0). The label has three outcomes for the bet's side: TP (+distance), SL (-distance)
and FLAT at the time barrier. Its fills already pay the spread and slippage, so a FLAT
with no drift realises minus those fills. `expected_value` is linear in the forecast's
class probabilities, so it prices the label exactly when each vertex (TP, SL or FLAT
certain) equals that outcome's realised P&L after the same costs: the same barrier
distance (TARGET_ATR x ATR_M15), the fills once, the commission once."""

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.support.decision import COSTS, CYCLE, T0, D, _ctx, _forecast, _proposal

from app.core.bet import LABEL_VERSION, SLIPPAGE, STRESSED_COST, UnitCost, cost_over_atr
from app.core.contracts import CandleClosed, ClassProbabilities
from app.core.enums import Direction, Timeframe
from app.decision.fusion import CENT, TARGET_ATR, Evidence, expected_value, gather
from app.learning.bars import Horizon
from app.learning.labels import Outcome, TripleBarrierLabel, time_bars, triple_barrier
from app.market_data.features import ATR_M15

M5 = timedelta(minutes=5)
CTX = _ctx()  # bid 2000, spread 0.20, ATR_M15 4, 0.01 lot of 100 oz
ATR = D(4)
START = T0 - M5  # the entry bar closes at the snapshot's as_of
SIDES = [Direction.UP, Direction.DOWN]


def _bar(i: int, *, high: str = "2000", low: str = "2000") -> CandleClosed:
    t = START + i * M5
    return CandleClosed(
        symbol="XAUUSD", timeframe=Timeframe.M5, open_time=t, close_time=t + M5,
        open=D(2000), high=D(high), low=D(low), close=D(2000), volume=None,
        spread=CTX.spread, available_at=t + M5,
    )  # fmt: skip


def _label(path: list[CandleClosed]) -> TripleBarrierLabel:
    f = _forecast()
    n = time_bars(Horizon(f.horizon, f.horizon_bars), Timeframe.M5)  # the forecast's span
    assert CTX.value(ATR_M15) == ATR and len(path) == n
    lab = triple_barrier(_bar(0), path, ATR, time_bars=n)
    assert lab is not None
    return lab


def _path(direction: Direction | None) -> list[CandleClosed]:
    """Twelve M5 bars; the first runs 10 ATR in `direction` (None: no drift at all)."""
    first = (_bar(1, high="2040") if direction is Direction.UP
             else _bar(1, low="1960") if direction is Direction.DOWN else _bar(1))  # fmt: skip
    return [first, *(_bar(i) for i in range(2, 13))]


def _ev(direction: Direction, side: str, opposite: str, flat: str,
        label_version: str = LABEL_VERSION) -> Decimal | None:  # fmt: skip
    up, down = (side, opposite) if direction is Direction.UP else (opposite, side)
    f = _forecast(p=side if Decimal(side) > 0 else "0.5", direction=direction).model_copy(
        update={"class_probabilities": ClassProbabilities(
            label_version=label_version, up=D(up), down=D(down), flat=D(flat))})  # fmt: skip
    inp = gather(CTX, [_proposal(direction=direction)], Evidence(forecast=f), cycle=CYCLE,
                 costs=COSTS, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    return expected_value(CTX, inp, COSTS)[0]


def _usd(pnl: Decimal) -> Decimal:
    """Per-unit P&L to USD for the initial lot, after the round-trip commission."""
    lot = CTX.initial_lot
    return (pnl * lot * COSTS.contract_size - 2 * COSTS.commission_per_lot * lot).quantize(CENT)


def _opposite(d: Direction) -> Direction:
    return Direction.DOWN if d is Direction.UP else Direction.UP


@pytest.mark.parametrize("direction", SIDES)
def test_each_certain_outcome_is_priced_at_the_labels_realised_payoff(
    direction: Direction,
) -> None:
    tp = _label(_path(direction)).side(direction)
    sl = _label(_path(_opposite(direction))).side(direction)
    flat = _label(_path(None)).side(direction)
    assert (tp.outcome, sl.outcome, flat.outcome) == (direction, _opposite(direction),
                                                      Outcome.FLAT)  # fmt: skip
    assert _ev(direction, "1", "0", "0") == _usd(tp.pnl)
    assert _ev(direction, "0", "1", "0") == _usd(sl.pnl)
    assert _ev(direction, "0", "0", "1") == _usd(flat.pnl)  # the fills, with no drift
    # linear in between: the three payoffs weighted by their probabilities
    weighted = D("0.5") * tp.pnl + D("0.2") * sl.pnl + D("0.3") * flat.pnl
    assert _ev(direction, "0.5", "0.2", "0.3") == _usd(weighted)


def test_the_barrier_is_the_distance_ev_prices_and_flat_pays_the_fills() -> None:
    lab = _label(_path(Direction.UP))
    assert lab.distance == TARGET_ATR * ATR
    assert lab.long.pnl == lab.distance and lab.short.pnl == -lab.distance
    assert CTX.spread is not None
    fills = UnitCost(spread=CTX.spread, slippage=SLIPPAGE, commission=D(0)).fills
    assert _label(_path(None)).long.pnl == -fills == _label(_path(None)).short.pnl


def test_flat_mass_is_not_priced_as_an_even_bet() -> None:
    # (2p - 1) on P(side) alone would say 0.6 - 0.4 = 0.2; the edge is 0.6 - 0.1 = 0.5,
    # and the 0.3 FLAT pays the fills
    ev = _ev(Direction.UP, "0.6", "0.1", "0.3")
    assert ev == (D(4) * D("0.5") - D("0.3") * D("0.30") - D("0.07")).quantize(CENT)


def test_without_class_probabilities_of_this_label_ev_is_refused() -> None:
    inp = gather(CTX, [_proposal()], Evidence(forecast=_forecast(classes=False)), cycle=CYCLE,
                 costs=COSTS, symbol="XAUUSD", policy_version="p1")  # fmt: skip
    assert expected_value(CTX, inp, COSTS) == (None, None)
    assert _ev(Direction.UP, "0.6", "0.1", "0.3", label_version="m07.labels.tb.v0") is None


@pytest.mark.parametrize("flat", ["0", "0.3", "0.9"])
def test_ev_breaks_even_where_the_edge_meets_cost_over_atr(flat: str) -> None:
    assert CTX.spread is not None
    cost = UnitCost(spread=CTX.spread, slippage=SLIPPAGE,
                    commission=2 * COSTS.commission_per_lot / COSTS.contract_size)  # fmt: skip
    hurdle = cost_over_atr(cost, ATR, flat=D(flat))
    rest = 1 - D(flat)
    side, opposite = (rest + hurdle) / 2, (rest - hurdle) / 2
    assert _ev(Direction.UP, str(side), str(opposite), flat) == 0
    assert cost_over_atr(cost, ATR) >= hurdle  # flat = 1 is the conservative hurdle


def test_the_stressed_cost_is_pre_declared_and_dearer_than_the_live_one() -> None:
    assert (STRESSED_COST.spread, STRESSED_COST.slippage, STRESSED_COST.commission) == (
        D("0.80"), D("0.10"), D("0.07"))  # fmt: skip
    assert CTX.spread is not None and STRESSED_COST.fills > CTX.spread + 2 * SLIPPAGE
