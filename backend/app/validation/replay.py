"""Point-in-time replay harness (#67; VAL-02, AT-15, AT-25; master plan §11.1).

Steps through stored bars of one driving timeframe in the order they became known.
At each bar's `available_at` it reads only bars available then (`CandleStore.bars`
with `as_of`), builds the real M02 feature snapshot, runs the real M05 gate,
strategies and `decide`, and simulates any ENTER with an explicit cost model:
- entry at the bar close, plus half the spread and the slippage against the trade;
- exit at the close of the first driving bar ending at or after the horizon, minus
  half the spread and the slippage, again against the trade;
- a round-trip commission.
Fills never improve on the bar's range. Evidence (forecasts, news) comes from a
provider that is itself point-in-time; by default there is none, and then M05 waits,
which is the honest result without a calibrated model.

The output is a run record, not a claim: decisions, simulated trades, metrics, the
no-trade baseline, and the list of simplifications this replay makes.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.contracts import CandleClosed, DecisionCreated
from app.core.enums import DecisionAction, Mode, Side, Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.decision.context import DecisionContext
from app.decision.decide import decide
from app.decision.fusion import Costs, CycleView, Evidence
from app.decision.gate import Strategy, eligible
from app.market_data.features import build_feature_snapshot
from app.market_data.store import CandleStore
from app.storage.registry import ArtifactRegistry
from app.validation.metrics import Metric, expectancy, max_drawdown, profit_factor, win_rate

D = Decimal
CENT = D("0.01")
LOOKBACK = 200
SIMPLIFICATIONS = (
    "fills at bar close plus half the spread and fixed slippage; no intrabar path",
    "exit at the first driving-bar close at or after the horizon; no stop or target inside",
    "one position at a time; no adds, partial fills, rejections or requotes",
    "no swap, no latency beyond bar availability, no margin or lot-step checks",
    "spread from the bar when present, else the cost model's",
)

EvidenceProvider = Callable[[datetime], Evidence]


@dataclass(frozen=True)
class CostModel:
    """Every cost is explicit: a replay states what it charged."""

    contract_size: Decimal
    commission_per_lot: Decimal
    slippage: Decimal
    """Price units, always against the trade."""
    default_spread: Decimal
    """Used when a bar carries no spread."""

    @property
    def costs(self) -> Costs:
        return Costs(self.contract_size, self.commission_per_lot)


@dataclass(frozen=True)
class SimTrade:
    decision_id: str
    side: Side
    lot: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    net_usd: Decimal


@dataclass(frozen=True)
class ReplayResult:
    decisions: list[DecisionCreated]
    trades: list[SimTrade]
    metrics: dict[str, Metric]
    baseline: dict[str, Metric]
    steps: int
    max_bar_available_at: list[datetime] = field(repr=False)
    """Per step, the latest `available_at` among the bars read: never after the step."""
    simplifications: tuple[str, ...] = SIMPLIFICATIONS


def _metrics(trades: Sequence[Decimal]) -> dict[str, Metric]:
    return {
        "expectancy": expectancy(trades),
        "win_rate": win_rate(trades),
        "profit_factor": profit_factor(trades),
        "max_drawdown": max_drawdown(trades),
    }


@dataclass
class _Open:
    decision_id: str
    side: Side
    lot: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_after: datetime


def _fill(bar: CandleClosed, side: Side, *, opening: bool, cost: CostModel) -> Decimal:
    """The bar close moved against the trade by half the spread plus slippage, and never
    better than the bar allows (a buy no lower than its low, a sell no higher than its
    high)."""
    half = (bar.spread if bar.spread is not None else cost.default_spread) / 2
    buying = (side is Side.BUY) == opening
    if buying:
        return max(bar.close + half + cost.slippage, bar.low)
    return min(bar.close - half - cost.slippage, bar.high)


def replay(
    candles: CandleStore,
    *,
    symbol: str,
    timeframe: Timeframe,
    context_timeframes: Iterable[Timeframe],
    start: datetime,
    end: datetime,
    strategies: Sequence[Strategy],
    enabled: Iterable[str],
    registry: ArtifactRegistry,
    initial_lot: Decimal,
    cost: CostModel,
    evidence: EvidenceProvider = lambda _t: Evidence(),
    policy_version: str = "replay",
) -> ReplayResult:
    enabled = list(enabled)
    tfs = list(dict.fromkeys([timeframe, *context_timeframes]))
    driving = [b for b in candles.bars(symbol, timeframe, as_of=end) if start <= b.open_time < end]
    decisions: list[DecisionCreated] = []
    trades: list[SimTrade] = []
    seen: list[datetime] = []
    open_: _Open | None = None
    last_closed: datetime | None = None
    for bar in driving:
        now = bar.available_at
        if open_ is not None and bar.close_time >= open_.exit_after:
            exit_price = _fill(bar, open_.side, opening=False, cost=cost)
            sign = D(1) if open_.side is Side.BUY else D(-1)
            size = open_.lot * cost.contract_size
            net = ((exit_price - open_.entry_price) * sign * size
                   - 2 * cost.commission_per_lot * open_.lot).quantize(CENT)  # fmt: skip
            trades.append(SimTrade(open_.decision_id, open_.side, open_.lot, open_.entry_time,
                                   open_.entry_price, now, exit_price, net))  # fmt: skip
            open_, last_closed = None, now
        bars_by_tf = {tf: candles.bars(symbol, tf, as_of=now, limit=LOOKBACK) for tf in tfs}
        seen.append(max(b.available_at for bs in bars_by_tf.values() for b in bs))
        snapshot = build_feature_snapshot(symbol, {k: v for k, v in bars_by_tf.items() if v}, now)
        spread = bar.spread if bar.spread is not None else cost.default_spread
        ctx = DecisionContext(now=now, mode=Mode.SHADOW, snapshot=snapshot, price=bar.close,
                              spread=spread, initial_lot=initial_lot)  # fmt: skip
        gate = eligible(strategies, ctx, enabled=enabled, registry=registry)
        ctx = gate.with_allowed(ctx)
        proposals = [p for s in gate.allowed if (p := s.propose(ctx)) is not None]
        cycle = CycleView("replay", 1 if open_ else 0, last_closed)
        d = decide(ctx, proposals, evidence(now), cycle=cycle, costs=cost.costs, symbol=symbol,
                   policy_version=policy_version)  # fmt: skip
        decisions.append(d.created)
        if d.created.action is DecisionAction.ENTER and d.intent is not None and d.intent.side:
            side = d.intent.side
            entry = _fill(bar, side, opening=True, cost=cost)
            chosen = next(p for p in proposals if p.strategy_id == d.created.strategy_id)
            horizon = TIMEFRAME_DURATION[chosen.horizon]
            open_ = _Open(d.created.decision_id, side, initial_lot, now, entry, now + horizon)
    nets = [t.net_usd for t in trades]
    return ReplayResult(
        decisions=decisions,
        trades=trades,
        metrics=_metrics(nets),
        baseline=_metrics([]),
        steps=len(driving),
        max_bar_available_at=seen,
    )
