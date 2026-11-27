"""Point-in-time replay harness (#67; VAL-02, AT-15, AT-25; master plan §11.1).

Steps through stored bars of one driving timeframe in the order they became known.
At each bar's `available_at` it reads only bars available then (`CandleStore.bars`
with `as_of`), builds the real M02 feature snapshot, runs the real M05 gate,
strategies and `decide`, and simulates any ENTER with an explicit cost model:
- entry at the bar close, plus half the spread and the slippage against the trade;
- exit at the close of the first driving bar ending at or after the horizon, minus
  half the spread and the slippage, again against the trade;
- a round-trip commission.
Costs are never negative, so a fill never improves on the close. Evidence (forecasts,
news) comes from a provider that is itself point-in-time; by default there is none,
and then M05 waits, which is the honest result without a calibrated model.

The output is a run record, not a claim: decisions, simulated trades, metrics, the
no-trade baseline, and the list of simplifications this replay makes.
"""

import bisect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.contracts import CandleClosed, DecisionCreated
from app.core.enums import DecisionAction, Mode, Side, Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.decision.context import DecisionContext
from app.decision.decide import decide
from app.decision.fusion import Costs, CycleView, Evidence
from app.decision.gate import Strategy, eligible
from app.market_data.features import DEFAULT_TECHNIQUES, build_feature_snapshot
from app.market_data.session import SessionSchedule
from app.market_data.store import CandleStore
from app.storage.registry import ArtifactRegistry
from app.validation.metrics import Metric, expectancy, max_drawdown, profit_factor, win_rate

D = Decimal
CENT = D("0.01")
LOOKBACK = max(t.lookback for t in DEFAULT_TECHNIQUES)
"""Exactly what the slowest default technique reads; more would be wasted parsing."""
AVAILABILITY_SLACK = timedelta(minutes=1)
"""How long after its close a bar may still arrive and be replayed."""
SIMPLIFICATIONS = (
    "fills at bar close plus half the spread and fixed slippage; no intrabar path",
    "exit at the first driving-bar close at or after the horizon; no stop or target inside",
    "one position at a time; no adds, partial fills, rejections or requotes",
    "while a position is held, M05 manage (EXIT/ADD/HOLD) is not simulated: exit is at the horizon",
    "a position still open when the window ends is closed at the last bar (forced_exit)",
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

    def __post_init__(self) -> None:
        if min(self.commission_per_lot, self.slippage, self.default_spread) < 0:
            raise ValueError("costs cannot be negative: a cost never helps a trade")
        if self.contract_size <= 0:
            raise ValueError("contract size must be positive")

    def spread(self, bar: CandleClosed) -> Decimal:
        return bar.spread if bar.spread is not None else self.default_spread

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
    forced_exit: bool = False
    """Closed early because the replay window ended, not at its horizon."""


@dataclass(frozen=True)
class ReplayResult:
    decisions: list[DecisionCreated]
    trades: list[SimTrade]
    metrics: dict[str, Metric]
    baseline: dict[str, Metric]
    max_bar_available_at: list[datetime] = field(repr=False)
    """Per step, the latest `available_at` among the bars read: never after the step."""
    simplifications: tuple[str, ...] = SIMPLIFICATIONS

    @property
    def steps(self) -> int:
        return len(self.decisions)


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
    """The bar close moved against the trade by half the spread plus slippage. Costs are
    non-negative (CostModel refuses otherwise), so a fill is never better than the close."""
    half = cost.spread(bar) / 2
    against = half + cost.slippage
    return bar.close + against if (side is Side.BUY) == opening else bar.close - against


def _settle(o: "_Open", bar: CandleClosed, cost: CostModel, *, forced: bool) -> SimTrade:
    exit_price = _fill(bar, o.side, opening=False, cost=cost)
    sign = D(1) if o.side is Side.BUY else D(-1)
    net = ((exit_price - o.entry_price) * sign * o.lot * cost.contract_size
           - 2 * cost.commission_per_lot * o.lot).quantize(CENT)  # fmt: skip
    return SimTrade(o.decision_id, o.side, o.lot, o.entry_time, o.entry_price,
                    bar.available_at, exit_price, net, forced)  # fmt: skip


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
    session: SessionSchedule | None = None,
) -> ReplayResult:
    enabled = list(enabled)
    tfs = list(dict.fromkeys([timeframe, *context_timeframes]))
    # every bar opening in [start, end), including the last one, which only becomes
    # available after `end`
    known_by = end + TIMEFRAME_DURATION[timeframe] + AVAILABILITY_SLACK
    # each timeframe is read once; every step then slices what was available at its time
    history = {tf: candles.bars(symbol, tf, as_of=known_by) for tf in tfs}
    available = {tf: [b.available_at for b in bars] for tf, bars in history.items()}
    driving = [b for b in history[timeframe] if start <= b.open_time < end]
    decisions: list[DecisionCreated] = []
    trades: list[SimTrade] = []
    seen: list[datetime] = []
    open_: _Open | None = None
    last_closed: datetime | None = None
    for bar in driving:
        now = bar.available_at
        if open_ is not None and bar.close_time >= open_.exit_after:
            trades.append(_settle(open_, bar, cost, forced=False))
            open_, last_closed = None, now
        bars_by_tf = {}
        for tf in tfs:
            i = bisect.bisect_right(available[tf], now)
            bars_by_tf[tf] = history[tf][max(0, i - LOOKBACK) : i]
        seen.append(max(b.available_at for bs in bars_by_tf.values() for b in bs))
        snapshot = build_feature_snapshot(symbol, {k: v for k, v in bars_by_tf.items() if v}, now,
                                          session=session)  # fmt: skip
        spread = cost.spread(bar)
        ctx = DecisionContext(now=now, mode=Mode.SHADOW, snapshot=snapshot, price=bar.close,
                              spread=spread, initial_lot=initial_lot)  # fmt: skip
        gate = eligible(strategies, ctx, enabled=enabled, registry=registry)
        ctx = gate.with_allowed(ctx)
        proposals = [p for s in gate.allowed if (p := s.propose(ctx)) is not None]
        cycle = CycleView("replay", 1 if open_ else 0, last_closed)
        d = decide(ctx, proposals, evidence(now), cycle=cycle, costs=cost.costs, symbol=symbol,
                   policy_version=policy_version)  # fmt: skip
        decisions.append(d.created)
        intent = d.intent
        if d.created.action is DecisionAction.ENTER and intent is not None:
            if intent.side is None or intent.requested_lot is None:
                raise ValueError("an ENTER intent must carry side and lot")
            entry = _fill(bar, intent.side, opening=True, cost=cost)
            hold_for = TIMEFRAME_DURATION[d.horizon or bar.timeframe]  # ENTER always has one
            open_ = _Open(d.created.decision_id, intent.side, intent.requested_lot, now, entry,
                          bar.close_time + hold_for)  # fmt: skip
    if open_ is not None and driving:  # the window ended first: close at the last bar
        trades.append(_settle(open_, driving[-1], cost, forced=True))
    nets = [t.net_usd for t in trades]
    return ReplayResult(
        decisions=decisions,
        trades=trades,
        metrics=_metrics(nets),
        baseline=_metrics([]),
        max_bar_available_at=seen,
    )
