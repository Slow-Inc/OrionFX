"""System-tier backtest (#131, PRD #120 slice 4; tier 2 in `docs/agents/training-loop.md`).

It replays model-driven decisions through the real chain over stored bars: an
evaluation-loaded model (`load_for_evaluation`) -> `EvaluationEvidence` -> `DecisionLoop`
(M05) -> M06 `Engine` -> `FakeBroker`. M06 runs as it is, with its real exits and position
management: the cycle Profit Target, basket trailing, the AI exit (a CLOSE_ALL latches
CLOSING), the drawdown lock and the Emergency Stop. Nothing here changes a risk rule,
threshold or exit; the backtest only feeds bars and reads the result.

One step per driving bar, at its `available_at` (the clock follows bar availability):
1. the broker's quote is the bar: bid = close (bars are bid prices), ask = bid + spread;
2. control commands due by then are journaled (e.g. EMERGENCY_STOP);
3. M06 ticks: drawdown, Profit Target, trailing, the close workflow, a positions snapshot;
4. the loop decides on the bars available then and journals any intent;
5. M06 ticks again: it vets the intent and executes what it allows.

- **Fresh database per run** (in memory): M06's latest-by-seq reads cannot see another
  run. The fake broker starts with no position, so no Manual Position exists to touch,
  and only M06 writes to it.
- **Point in time.** Every bar read goes through `_PointInTime`, which raises `LookAhead`
  on any bar or query later than the step. Regime tags use only days that had ended.
- **Live paths never run.** Only an `EvaluationPredictor` is accepted; `DecisionLoop`
  itself still refuses `EvaluationEvidence`. `_EvaluationLoop` is the one override of the
  loop's evidence seam.
- **Costs** (`BacktestCost`): every fill is worse than the bar by the spread (on the ask
  side) and the slippage, and each deal pays commission. STRESSED, the default for the
  protocol gates, takes `STRESSED_COST` from `app.core.bet` and never lets a narrower bar
  spread make it cheaper. MODELLED takes the bar's spread and a caller-supplied cost; its
  values are TBD until Demo measures them. On a ratio-adjusted series (FirstRate GC, #128)
  a `PriceScale` f(t) charges the fixed-dollar cost as cost x f(t) in the series' units.
- **NO_TRADE bars** (#128 fills minutes with no trade flat) are not quotes: the last quote
  stays and goes stale, so such a bar never triggers an entry or an exit by itself.
- **Determinism.** `output_hash` covers the whole result except wall-clock timings. Ids
  that M06 draws at random (cycles, requests) are replaced by ordinals; broker position
  ids are sequential.

A BACKTEST IS NOT A REAL-MARKET RESULT. It is the system's behaviour on stored bars under
the simplifications listed in `SIMPLIFICATIONS`; only Shadow/Demo on the real market can
confirm it (#17). Its metrics are never a profit claim.
"""

import bisect
import json
import random
import sqlite3
import time as wallclock
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from app.core.bet import STRESSED_COST, UnitCost
from app.core.config import TradingConfig, UserConfig
from app.core.contracts import (
    CandleClosed,
    ControlRequested,
    DecisionCreated,
    EligibilityRecord,
    FeatureSnapshot,
    HealthUpdated,
    MarketSnapshot,
    RiskDecision,
    TradeIntent,
    sha256_ref,
)
from app.core.enums import (
    ControlCommand,
    DataQuality,
    Eligibility,
    HealthStatus,
    IntentAction,
    Mode,
    RiskProfile,
    Side,
    Timeframe,
    Verdict,
)
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import TIMEFRAME_DURATION, fresh
from app.decision.fusion import Costs, Evidence
from app.decision.gate import Strategy
from app.engine.engine import Engine, EngineSettings
from app.execution.broker import Deal, Position
from app.execution.fake_broker import FakeBroker
from app.execution.ownership import OwnershipRegistry
from app.learning.experience import ExperienceLog
from app.market_data.candles import QUOTE_MAX_AGE
from app.market_data.session import SessionSchedule
from app.market_data.store import CandleStore
from app.prediction.card import CARD_PIN
from app.prediction.engine import EvaluationPredictor, Refused
from app.profit.cycle import CycleBook
from app.risk.drawdown import dd_percent
from app.risk.policy import DEFAULT_POLICY, RiskPolicy
from app.runtime.loop import DecisionLoop
from app.storage.config_store import ConfigStore
from app.storage.db import connect, migrate
from app.storage.journal import Journal, StoredEvent, payload_as
from app.storage.registry import ArtifactRegistry, subject_ref
from app.validation.evidence import ValidationRun
from app.validation.metrics import Metric, expectancy, max_drawdown, profit_factor, win_rate
from app.validation.regimes import (
    Coverage,
    CoverageFloor,
    DailyBar,
    RegimeClassifier,
    protocol_v1_draft,
)
from app.validation.replay import AVAILABILITY_SLACK, EvaluationEvidence

D = Decimal
FLOOR = CoverageFloor()
SOURCE = "x02.backtest"
ACTOR = "x02-system-backtest"
SETTINGS = EngineSettings(holder=ACTOR)
BASELINE_STRATEGY = "random_entry"
NO_TRADE = "NO_TRADE"
"""The flag #128 adds as `QualityFlag.NO_TRADE` (not on this branch yet): a bar filled flat
from minutes with no trade. It is not a market quote, so the step keeps the last quote,
which goes stale: M05 decides nothing and M06 refuses entries on it (QUOTE_STALE), while
its exits still run at the last price."""
HISTORY = timedelta(days=420)
"""Calendar days read before the window for the regime tags (252 + 20 trading days)."""
SIMPLIFICATIONS = (
    "fills at the bar close: bid = close, ask = close + spread, each fill worse by the "
    "slippage; no intrabar path",
    "M06 sees one quote per driving bar: Profit Target, trailing and drawdown are checked "
    "at bar closes only",
    "the Emergency SL is not exercised: M06 never sets OpenRequest.emergency_sl and the "
    "fake broker has no stops",
    "M05's EV prices slippage at bet.SLIPPAGE and commission at the window start's price "
    "scale, which can differ from what the fills pay",
    "strategy approvals are granted (DEMO) inside the run's own database; the model is "
    "evaluation-loaded and never approved; the real registry is never read or written",
    "no swap, margin call, partial fill, rejection, requote or latency beyond bar availability",
    "positions still open at the end are reported separately, never closed or counted",
    "no news evidence (no point-in-time archive); news and stress windows not bucketed",
    "under a price scale, USD results and M06's dollar thresholds (Profit Target, "
    "max_spread, trailing, drawdown) are in the series' adjusted units",
)


class LookAhead(RuntimeError):
    """A step read a bar that was not available yet: the run is invalid."""


class CostMode(StrEnum):
    STRESSED = "STRESSED"
    MODELLED = "MODELLED"


@dataclass(frozen=True, slots=True)
class PriceScale:
    """The cumulative ratio-adjustment factor f(t) of a back-adjusted series (FirstRate
    GC, #128): older prices are multiplied by f <= 1. A fixed-dollar cost is charged in
    adjusted units as cost x f(t), so it stays fixed in real dollars. `steps` are
    (from, factor) in time order; f(t) is the factor of the last step at or before t,
    and a time before the first step is refused, never guessed."""

    ref: str
    """What the factors are, for the manifest (e.g. the dataset version they came from)."""
    steps: tuple[tuple[datetime, Decimal], ...]

    def __post_init__(self) -> None:
        starts = [t for t, _ in self.steps]
        if not starts or starts != sorted(set(starts)):
            raise ValueError("price scale steps must be non-empty and strictly in time order")
        if any(f <= 0 for _, f in self.steps):
            raise ValueError("a price scale factor must be positive")

    def at(self, t: datetime) -> Decimal:
        i = bisect.bisect_right(self.steps, t, key=lambda s: s[0]) - 1
        if i < 0:
            raise ValueError(f"{self.ref}: no price scale factor at {t.isoformat()}")
        return self.steps[i][1]


@dataclass(frozen=True, slots=True)
class BacktestCost:
    mode: CostMode
    unit: UnitCost
    """In real dollars: the spread (the fallback or, when stressed, the floor), the
    slippage per fill, and the round-trip commission per unit."""
    scale: PriceScale | None = None
    """f(t) for a ratio-adjusted series; None is f = 1."""

    def factor(self, t: datetime) -> Decimal:
        return D(1) if self.scale is None else self.scale.at(t)

    def spread(self, bar: CandleClosed) -> Decimal:
        """In the bar's (adjusted) price units."""
        own = self.unit.spread * self.factor(bar.open_time)
        if bar.spread is None:
            return own
        if self.mode is CostMode.STRESSED:
            return max(bar.spread, own)  # stress never gets cheaper
        return bar.spread

    def slippage(self, t: datetime) -> Decimal:
        return self.unit.slippage * self.factor(t)

    def commission_per_lot(self, contract_size: Decimal, t: datetime) -> Decimal:
        """Per deal (one side): a round trip pays it twice."""
        return self.unit.commission * contract_size / 2 * self.factor(t)


STRESSED = BacktestCost(CostMode.STRESSED, STRESSED_COST)


def modelled(unit: UnitCost, scale: PriceScale | None = None) -> BacktestCost:
    """The modelled cost: TBD until Demo measures it (protocol v1 §5, §9)."""
    return BacktestCost(CostMode.MODELLED, unit, scale)


@dataclass(frozen=True, slots=True)
class Account:
    """The simulated owner settings and starting balance."""

    balance: Decimal
    initial_lot: Decimal
    profit_target_usd: Decimal
    max_drawdown_percent: Decimal
    risk_profile: RiskProfile = RiskProfile.LOW


@dataclass(frozen=True)
class BacktestSpec:
    symbol: str
    timeframe: Timeframe
    """The driving timeframe: one step per bar."""
    context_timeframes: tuple[Timeframe, ...]
    start: datetime
    end: datetime
    """Bars opening in [start, end) are stepped."""
    account: Account
    cost: BacktestCost = STRESSED
    session: SessionSchedule | None = None
    floor: CoverageFloor = FLOOR
    baseline_seed: int | None = 0
    """None skips the random-entry baseline."""
    controls: tuple[tuple[datetime, ControlCommand], ...] = ()
    """Owner commands journaled at the first step at or after their time."""


# --- results ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    position: str
    cycle: int
    """The cycle's ordinal in this run (M06 draws cycle ids at random)."""
    side: Side
    lot: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    commission: Decimal
    """Both deals'."""
    net_usd: Decimal
    close_reason: str
    regime: dict[str, str]
    """Tags at entry."""


@dataclass(frozen=True)
class OpenPosition:
    position: str
    cycle: int
    side: Side
    lot: Decimal
    entry_time: datetime
    entry_price: Decimal
    floating_usd: Decimal
    """At the last quote, before the closing deal's slippage and commission."""


@dataclass(frozen=True)
class Refusal:
    time: datetime
    action: IntentAction
    side: Side | None
    reasons: list[str]


@dataclass(frozen=True)
class Bucket:
    dimension: str
    name: str
    trades: int
    days: int
    expectancy: Metric
    net_usd: Decimal
    coverage: Coverage


@dataclass(frozen=True)
class Summary:
    trades: int
    net_usd: Decimal
    expectancy: Metric
    profit_factor: Metric
    win_rate: Metric
    max_drawdown: Metric
    """Of the cumulative trade result (USD)."""
    close_reasons: dict[str, int]


@dataclass(frozen=True)
class Manifest:
    code_sha: str
    model: str
    card_pin: str
    weights_hash: str | None
    label_version: str | None
    dataset_version: str
    protocol_hash: str | None
    policy_version: str
    strategies: list[str]
    classifier: str
    spec: BacktestSpec


@dataclass(frozen=True)
class SystemBacktestResult:
    manifest: Manifest
    trades: list[Trade]
    open_at_end: list[OpenPosition]
    refused: list[Refusal]
    daily_pnl: list[tuple[date, Decimal]]
    """Equity change per trading day with a step, zero days included (DSR/PBO input)."""
    equity: list[tuple[datetime, Decimal]]
    summary: Summary
    max_drawdown_usd: Decimal
    max_drawdown_percent: Decimal
    """Of step equity, peak to trough."""
    exposure: Decimal
    """Share of steps that held a bot position."""
    regimes: list[Bucket]
    baselines: dict[str, Summary]
    decisions: dict[str, int]
    """M05's decisions by action and reason codes (WAITs included)."""
    no_decision: dict[str, int]
    """Steps where the loop decided nothing, by its health detail."""
    steps: int
    simplifications: tuple[str, ...]
    timings: dict[str, float] = field(compare=False)
    """Wall clock and M06 deal-scan cost; never hashed."""

    def body(self) -> dict[str, Any]:
        out = asdict(self)
        del out["timings"]
        return out

    @property
    def output_hash(self) -> str:
        blob = json.dumps(self.body(), default=str, sort_keys=True, separators=(",", ":"))
        return sha256_ref(blob.encode())

    def expectancy_ci(
        self, estimator: Callable[[list[list[Decimal]]], tuple[Decimal, Decimal]]
    ) -> tuple[Decimal, Decimal]:
        """The CI hook: `estimator` gets the trade nets grouped by exit trading day, in
        day order (a block bootstrap over days resamples these groups)."""
        days: dict[date, list[Decimal]] = defaultdict(list)
        for t in self.trades:
            days[trading_day(t.exit_time, self.manifest.spec.session)].append(t.net_usd)
        return estimator([days[d] for d in sorted(days)])

    def validation_run(self) -> ValidationRun:
        """The evidence record (`EvidenceStore.record_run`). Only covered buckets are
        listed as regimes: the store counts every listed regime as covered."""
        spec = self.manifest.spec
        return ValidationRun(
            kind="system_backtest",
            subject_refs=[f"model:{self.manifest.model}",
                          *(f"strategy:{s}" for s in self.manifest.strategies)],
            config={"output_hash": self.output_hash, "code_sha": self.manifest.code_sha,
                    "card_pin": self.manifest.card_pin,
                    "dataset_version": self.manifest.dataset_version,
                    "cost_mode": spec.cost.mode.value,
                    "protocol_hash": self.manifest.protocol_hash or ""},
            regimes=[f"{b.dimension}:{b.name}" for b in self.regimes
                     if b.coverage is Coverage.COVERED],
            window_start=spec.start, window_end=spec.end, trades=self.summary.trades,
            metrics={"expectancy": self.summary.expectancy,
                     "profit_factor": self.summary.profit_factor,
                     "win_rate": self.summary.win_rate,
                     "max_drawdown": self.summary.max_drawdown},
        )  # fmt: skip


# --- trading days ---------------------------------------------------------------------------


def _day_frame(session: SessionSchedule | None) -> tuple[ZoneInfo, timedelta]:
    """The zone and the shift that makes a trading day a calendar date: a day ends at the
    session's daily close (17:00 New York for XAUUSD), or at midnight UTC without one."""
    if session is None:
        return ZoneInfo("UTC"), timedelta(0)
    c = session.daily_close
    return ZoneInfo(session.tz), timedelta(days=1) - timedelta(hours=c.hour, minutes=c.minute)


def trading_day(t: datetime, session: SessionSchedule | None) -> date:
    tz, shift = _day_frame(session)
    return (t.astimezone(tz) + shift).date()


def _day_end(day: date, session: SessionSchedule | None) -> datetime:
    tz, shift = _day_frame(session)
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=tz) - shift


def daily_bars(bars: Sequence[CandleClosed], session: SessionSchedule | None) -> list[DailyBar]:
    """One `DailyBar` per trading day, known once the day has ended and its last bar
    is available."""
    by_day: dict[date, list[CandleClosed]] = defaultdict(list)
    for b in bars:
        by_day[trading_day(b.open_time, session)].append(b)
    out = []
    for day in sorted(by_day):
        g = by_day[day]
        rv = sum(((c.close / p.close).ln() ** 2 for p, c in pairwise(g)), D(0))
        known = max(_day_end(day, session), g[-1].available_at)
        out.append(DailyBar(day, g[-1].close, rv.sqrt(), known))
    return out


# --- the simulation -----------------------------------------------------------------------


class _PointInTime(CandleStore):
    """Delegates every read to the source store and raises `LookAhead` if a read asks
    for, or returns, anything later than the current step. The loop catches its own
    faults, so the first violation is also kept, and the step fails the run on it."""

    def __init__(self, inner: CandleStore) -> None:
        super().__init__(inner.conn, dataset_version=inner.dataset_version)
        self._inner = inner
        self.now: datetime | None = None
        self.violation: str | None = None

    def _check(self, as_of: datetime, seen: Sequence[datetime]) -> None:
        if self.now is not None and (as_of > self.now or any(t > self.now for t in seen)):
            self.violation = self.violation or (
                f"read as of {as_of.isoformat()} at step {self.now.isoformat()}"
            )
            raise LookAhead(self.violation)

    def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        as_of: datetime,
        limit: int | None = None,
        after: datetime | None = None,
    ) -> list[CandleClosed]:
        out = self._inner.bars(symbol, timeframe, as_of=as_of, limit=limit, after=after)
        self._check(as_of, [b.available_at for b in out])
        return out

    def latest_open(self, symbol: str, timeframe: Timeframe, *, as_of: datetime) -> datetime | None:
        self._check(as_of, [])
        return self._inner.latest_open(symbol, timeframe, as_of=as_of)

    def bar_at(self, symbol: str, timeframe: Timeframe, open_time: datetime, *,
               as_of: datetime) -> CandleClosed | None:  # fmt: skip
        out = self._inner.bar_at(symbol, timeframe, open_time, as_of=as_of)
        self._check(as_of, [] if out is None else [out.available_at])
        return out


class _TimedBroker(FakeBroker):
    """Measures M06's full deal-history scan (`deals_since(None)`), per #121."""

    scans = 0
    scan_seconds = 0.0

    def deals_since(self, after_deal_id: str | None) -> list[Deal]:
        t0 = wallclock.perf_counter()
        try:
            return super().deals_since(after_deal_id)
        finally:
            if after_deal_id is None:
                self.scans += 1
                self.scan_seconds += wallclock.perf_counter() - t0


def _no_live_evidence(now: datetime) -> Evidence:
    raise AssertionError("the evaluation loop never asks a live evidence provider")


class _EvaluationLoop(DecisionLoop):
    """The real decision loop, fed an evaluation-loaded model's forecast on the snapshot
    each tick built. Built only by `run_system_backtest`, on its own fresh database and
    fake broker: it never sees a live account."""

    def __init__(self, conn: sqlite3.Connection, *, model: EvaluationEvidence,
                 **kw: Any) -> None:  # fmt: skip
        super().__init__(conn, evidence=_no_live_evidence, **kw)
        self._model = model

    def _gather_evidence(self, now: datetime, snapshot: FeatureSnapshot) -> Evidence:
        return self._model(now, snapshot)


class _Sim:
    """One run: a fresh in-memory database, a fake broker, M06 started in DEMO."""

    def __init__(self, spec: BacktestSpec, candles: _PointInTime, known: CandleClosed | None, *,
                 policy: RiskPolicy, enabled: list[str]) -> None:  # fmt: skip
        self.spec, self.candles = spec, candles
        self.conn = connect(":memory:")
        migrate(self.conn)
        self.journal = Journal(self.conn)
        # M06 starts at the window start on the last bar known then, if any; the first
        # step replaces that quote (or, on a NO_TRADE bar, lets it go stale).
        self.broker = _TimedBroker(balance=spec.account.balance, now=spec.start)
        self.contract = self.broker.specs[spec.symbol].contract_size
        if known is not None:
            self.broker.now = known.available_at
            self._quote(known)
            self.broker.now = spec.start
        a = spec.account
        trading = TradingConfig(symbol=spec.symbol, initial_lot=a.initial_lot,
                                profit_target_usd=a.profit_target_usd,
                                max_drawdown_percent=a.max_drawdown_percent,
                                account_mode=Mode.DEMO, risk_profile=a.risk_profile,
                                strategies_enabled=enabled)  # fmt: skip
        store = ConfigStore(self.conn)
        version, _ = store.current()
        store.update(UserConfig(trading=trading), expected_version=version, actor=ACTOR)
        clock = self.clock
        self.engine = Engine(self.conn, self.broker, settings=SETTINGS,
                             policy=policy, clock=clock)  # fmt: skip
        self.book = CycleBook(self.conn, account=self.broker.account(), clock=clock)
        self.owners = OwnershipRegistry(self.conn, clock=clock)
        self.engine.start()
        self._command(ControlCommand.START, "start")
        self.engine.tick()
        self.controls = sorted(spec.controls)
        self.equity: list[tuple[datetime, Decimal]] = []
        self.days: dict[date, Decimal] = {}
        self.held_steps = 0
        self.flat_steps = 0
        self.no_decision: Counter[str] = Counter()

    def clock(self) -> datetime:
        return self.broker.now

    def _quote(self, bar: CandleClosed) -> None:
        """The bar as the broker's quote, with the costs in the bar's price units."""
        cost, t = self.spec.cost, bar.open_time
        self.broker.slippage = cost.slippage(t)
        self.broker.commission_per_lot = cost.commission_per_lot(self.contract, t)
        self.broker.set_quote(self.spec.symbol, bid=bar.close, ask=bar.close + cost.spread(bar))

    def _command(self, command: ControlCommand, key: str) -> None:
        cid = stable_id("cmd", command, key)
        self.journal.append(make_event(
            "control.requested", source=SOURCE,
            payload=ControlRequested(command_id=cid, command=command, actor=ACTOR),
            account_id=SETTINGS.account_id, bot_id=SETTINGS.bot_id, mode=Mode.DEMO, event_id=cid,
            occurred_at=self.clock(),
        ))  # fmt: skip

    def bot_positions(self) -> list[Position]:
        return self.owners.bot_positions(self.broker.account(), self.broker.positions())

    def step(self, bar: CandleClosed, decide: Callable[[], bool]) -> None:
        """`decide` returns whether it decided anything this step."""
        now = bar.available_at
        self.candles.now = self.broker.now = now
        if NO_TRADE not in bar.quality_flags:
            self._quote(bar)  # a no-trade bar brings no quote: the last one goes stale
        while self.controls and self.controls[0][0] <= now:
            at, command = self.controls.pop(0)
            self._command(command, at.isoformat())
        self.engine.tick()
        flat = not self.bot_positions()
        self.flat_steps += flat
        if not decide():
            self._note_no_decision(now)
        self.engine.tick()
        if self.candles.violation is not None:
            raise LookAhead(self.candles.violation)
        equity = self.broker.account().equity
        self.equity.append((now, equity))
        self.days[trading_day(bar.open_time, self.spec.session)] = equity
        self.held_steps += bool(self.bot_positions())

    def _note_no_decision(self, now: datetime) -> None:
        latest = self.journal.recent(["health.updated"], limit=1)
        h = payload_as(latest[0], HealthUpdated) if latest else None
        if h is None or h.last_heartbeat != now:
            self.no_decision["no health reported"] += 1
        elif h.status is not HealthStatus.OK or h.detail:
            self.no_decision[f"{h.status.value}: {h.detail}"] += 1

    def events(self, *types: str) -> Iterator[StoredEvent]:
        seq = 0
        while batch := self.journal.read_since(seq, event_types=list(types)):
            yield from batch
            seq = batch[-1].seq


def _deny(intent_of: dict[str, TradeIntent], decision: RiskDecision) -> Refusal:
    intent = intent_of[decision.intent_id]
    return Refusal(decision.decided_at, intent.action, intent.side, decision.reason_codes)


def _vwap(deals: Sequence[Deal]) -> Decimal:
    volume = sum((d.volume for d in deals), D(0))
    return sum((d.volume * (d.price or D(0)) for d in deals), D(0)) / volume


@dataclass
class _Ledger:
    trades: list[Trade]
    open_at_end: list[OpenPosition]
    refused: list[Refusal]
    opens: int


def _ledger(sim: _Sim, tags: Callable[[datetime], dict[str, str]]) -> _Ledger:
    """Trades from the broker's deals, cross-checked against the balance and each
    closed cycle's final net; a mismatch is a backtest bug and fails the run."""
    account = sim.broker.account()
    ids = [r[0] for r in sim.conn.execute("SELECT cycle_id FROM cycles"
                                          " ORDER BY opened_at, rowid")]  # fmt: skip
    cycles = [sim.book.snapshot(c) for c in ids]
    cycle_of = {}
    for ordinal, c in enumerate(cycles):
        for pid, _ in sim.owners.positions_of_cycle(account, c.cycle_id):
            cycle_of[pid] = (ordinal, c.close_reason)
    deals: dict[str, list[Deal]] = defaultdict(list)
    for d in sim.broker.deals_since(None):
        if not d.is_balance and d.position_id is not None:
            deals[d.position_id].append(d)
    still = {p.position_id: p for p in sim.bot_positions()}
    trades, open_at_end = [], []
    by_cycle: dict[int, Decimal] = defaultdict(Decimal)
    for pid, ds in deals.items():
        entry, exits = ds[0], ds[1:]
        ordinal, reason = cycle_of[pid]
        if pid in still:
            p = still[pid]
            open_at_end.append(OpenPosition(pid, ordinal, p.side, p.volume, entry.time,
                                            entry.price or D(0), p.profit))  # fmt: skip
            continue
        if entry.side is None or entry.price is None:
            raise RuntimeError(f"backtest accounting: {pid} opened by a deal with no side")
        commission = sum((d.commission for d in ds), D(0))
        net = sum((d.profit for d in ds), D(0)) - commission
        trades.append(Trade(pid, ordinal, entry.side, entry.volume, entry.time, entry.price,
                            exits[-1].time, _vwap(exits), commission, net,
                            reason or "UNKNOWN", tags(entry.time)))  # fmt: skip
        by_cycle[ordinal] += net
    open_cost = sum((deals[pid][0].commission for pid in still), D(0))
    if (
        sum((t.net_usd for t in trades), D(0)) - open_cost
        != account.balance - sim.spec.account.balance
    ):
        raise RuntimeError("backtest accounting: trade nets do not match the balance")
    for ordinal, c in enumerate(cycles):
        if c.final_net_usd is not None and by_cycle[ordinal] != c.final_net_usd:
            raise RuntimeError(f"backtest accounting: cycle {ordinal} final net")
    intents = {i.intent_id: i for i in (payload_as(e, TradeIntent)
                                        for e in sim.events("trade.intent"))}  # fmt: skip
    decisions = [payload_as(e, RiskDecision) for e in sim.events("risk.decision")]
    refused = [_deny(intents, r) for r in decisions if r.verdict is Verdict.DENY]
    opens = sum(1 for r in decisions if r.verdict is Verdict.ALLOW
                and intents[r.intent_id].action is IntentAction.OPEN)  # fmt: skip
    return _Ledger(sorted(trades, key=lambda t: (t.entry_time, t.position)),
                   open_at_end, refused, opens)  # fmt: skip


def summarize(trades: Sequence[Trade]) -> Summary:
    nets = [t.net_usd for t in trades]
    return Summary(trades=len(trades), net_usd=sum(nets, D(0)), expectancy=expectancy(nets),
                   profit_factor=profit_factor(nets), win_rate=win_rate(nets),
                   max_drawdown=max_drawdown(nets),
                   close_reasons=dict(sorted(Counter(t.close_reason
                                                     for t in trades).items())))  # fmt: skip


def _buckets(trades: Sequence[Trade], floor: CoverageFloor,
             session: SessionSchedule | None) -> list[Bucket]:  # fmt: skip
    groups: dict[tuple[str, str], list[Trade]] = defaultdict(list)
    for t in trades:
        for dimension, name in t.regime.items():
            groups[dimension, name].append(t)
    out = []
    for (dimension, name), g in sorted(groups.items()):
        nets = [t.net_usd for t in g]
        days = len({trading_day(t.entry_time, session) for t in g})
        out.append(Bucket(dimension, name, len(g), days, expectancy(nets), sum(nets, D(0)),
                          floor.coverage(len(g), days)))  # fmt: skip
    return out


def _drawdown(
    equity: Sequence[tuple[datetime, Decimal]], start: Decimal
) -> tuple[Decimal, Decimal]:
    peak, usd, pct = start, D(0), D(0)
    for _, e in equity:
        peak = max(peak, e)
        usd = max(usd, peak - e)
        pct = max(pct, dd_percent(peak, e))
    return usd, pct


def _daily(days: dict[date, Decimal], start: Decimal) -> list[tuple[date, Decimal]]:
    out, last = [], start
    for day in sorted(days):
        out.append((day, days[day] - last))
        last = days[day]
    return out


def _random_entrant(sim: _Sim, rng: random.Random, p: float) -> Callable[[], bool]:
    """Seeded entries at the model run's basket rate per flat step, random side, same
    lot; M06 alone exits them. Never asks M05."""
    spec = sim.spec

    def decide() -> bool:
        if sim.bot_positions() or rng.random() >= p:
            return True
        cycle_id = sim.book.open_cycle_id()
        now = sim.clock()
        iid = stable_id("intent", BASELINE_STRATEGY, now)
        side = rng.choice((Side.BUY, Side.SELL))
        sim.journal.append(make_event(
            "trade.intent", source=SOURCE,
            payload=TradeIntent(
                intent_id=iid, decision_id=iid, action=IntentAction.OPEN, side=side,
                symbol=spec.symbol, requested_lot=spec.account.initial_lot,
                strategy_id=BASELINE_STRATEGY, cycle_id=cycle_id, mode=Mode.DEMO,
                expected_value_usd=None, prediction_ids=[], position_refs=[],
                valid_until=now + timedelta(seconds=30)),
            account_id=SETTINGS.account_id, bot_id=SETTINGS.bot_id, mode=Mode.DEMO,
            cycle_id=cycle_id, event_id=iid, occurred_at=now,
        ))  # fmt: skip
        return True

    return decide


def _approve(conn: sqlite3.Connection, strategies: Sequence[Strategy], spec: BacktestSpec,
             policy: RiskPolicy) -> ArtifactRegistry:  # fmt: skip
    """DEMO approvals dated before the window, in this run's own database only."""
    registry = ArtifactRegistry(conn, clock=lambda: spec.start - timedelta(days=1))
    for s in strategies:
        m = s.manifest
        registry.register("strategy", m, actor=ACTOR)
        registry.set_eligibility(EligibilityRecord(
            subject_hash=m.artifact_hash, subject_kind="strategy",
            subject_ref=subject_ref(m.strategy_id, m.version), user_enabled=True,
            eligibility=Eligibility.DEMO_ELIGIBLE, approved_modes=[Mode.DEMO], context={},
            evidence_refs=["x02:system-backtest"], policy_version=policy.policy_version,
            expires_at=None, revoked_at=None,
        ), actor=ACTOR)  # fmt: skip
    return registry


def run_system_backtest(
    candles: CandleStore,
    *,
    model: EvaluationPredictor,
    strategies: Sequence[Strategy],
    spec: BacktestSpec,
    code_sha: str,
    protocol_hash: str | None = None,
    regimes: RegimeClassifier = protocol_v1_draft,
    policy: RiskPolicy = DEFAULT_POLICY,
) -> SystemBacktestResult:
    if not isinstance(model, EvaluationPredictor):
        raise Refused("the system backtest takes only a model loaded by load_for_evaluation")
    card = model.predictor.card
    if card.training_cutoff is None or spec.start <= card.training_cutoff:
        raise Refused(f"{card.ref}: the window must start after the training cutoff")
    t0 = wallclock.perf_counter()
    guarded = _PointInTime(candles)
    tf = spec.timeframe
    known_by = spec.end + TIMEFRAME_DURATION[tf] + AVAILABILITY_SLACK
    history = candles.bars(spec.symbol, tf, as_of=known_by, after=spec.start - HISTORY)
    driving = [b for b in history if spec.start <= b.open_time < spec.end]
    if not driving:
        raise ValueError("no driving bar in the window")
    opening = next((b for b in reversed(history) if b.available_at <= spec.start), None)
    days = daily_bars(history, spec.session)
    known = [d.known_at for d in days]

    def tags(t: datetime) -> dict[str, str]:
        return dict(regimes(t, days[: bisect.bisect_right(known, t)]))

    enabled = [s.manifest.strategy_id for s in strategies]
    sim = _Sim(spec, guarded, opening, policy=policy, enabled=enabled)
    loop = _EvaluationLoop(
        sim.conn, model=EvaluationEvidence(model, guarded, spec.session), candles=guarded,
        symbol=spec.symbol, timeframe=tf, context_timeframes=spec.context_timeframes,
        strategies=strategies, registry=_approve(sim.conn, strategies, spec, policy),
        quote=lambda now: _market(sim, now),
        costs=Costs(sim.contract, spec.cost.commission_per_lot(sim.contract, spec.start)),
        experiences=ExperienceLog(sim.conn, guarded, clock=sim.clock), policy=policy,
        clock=sim.clock, session=spec.session,
    )  # fmt: skip
    for bar in driving:
        sim.step(bar, lambda: loop.tick() is not None)
    ledger = _ledger(sim, tags)
    stepped = wallclock.perf_counter() - t0
    baselines = {"no_trade": summarize([])}
    if spec.baseline_seed is not None:
        p = ledger.opens / sim.flat_steps if sim.flat_steps else 0.0
        base = _Sim(spec, guarded, opening, policy=policy, enabled=[BASELINE_STRATEGY])
        entrant = _random_entrant(base, random.Random(spec.baseline_seed), p)
        for bar in driving:
            base.step(bar, entrant)
            baselines[BASELINE_STRATEGY] = summarize(_ledger(base, lambda _t: {}).trades)
    start = spec.account.balance
    dd_usd, dd_pct = _drawdown(sim.equity, start)
    return SystemBacktestResult(
        manifest=Manifest(
            code_sha=code_sha, model=card.ref, card_pin=CARD_PIN + card.content_hash,
            weights_hash=card.weights_hash, label_version=card.label_version,
            dataset_version=candles.dataset_version, protocol_hash=protocol_hash,
            policy_version=policy.policy_version,
            strategies=[subject_ref(s.manifest.strategy_id, s.manifest.version)
                        for s in strategies],
            classifier=getattr(regimes, "__qualname__", repr(regimes)), spec=spec),
        trades=ledger.trades, open_at_end=ledger.open_at_end, refused=ledger.refused,
        daily_pnl=_daily(sim.days, start), equity=sim.equity,
        summary=summarize(ledger.trades), max_drawdown_usd=dd_usd,
        max_drawdown_percent=dd_pct, exposure=D(sim.held_steps) / len(driving),
        regimes=_buckets(ledger.trades, spec.floor, spec.session), baselines=baselines,
        decisions=_decisions(sim), no_decision=dict(sorted(sim.no_decision.items())),
        steps=len(driving),
        simplifications=SIMPLIFICATIONS,
        timings={"model_run_seconds": stepped, "steps_per_second": len(driving) / stepped,
                 "deal_scans": sim.broker.scans,
                 "deal_scan_seconds": sim.broker.scan_seconds,
                 "total_seconds": wallclock.perf_counter() - t0},
    )  # fmt: skip


def _decisions(sim: _Sim) -> dict[str, int]:
    made = (payload_as(e, DecisionCreated) for e in sim.events("decision.created"))
    keys = Counter(f"{d.action.value}:{','.join(d.reason_codes)}" for d in made)
    return dict(sorted(keys.items()))


def _market(sim: _Sim, now: datetime) -> MarketSnapshot:
    """The loop's quote: the broker's, which the step set from the bar at `now`."""
    q = sim.broker.quote(sim.spec.symbol)
    if q is None:
        return MarketSnapshot(symbol=sim.spec.symbol, bid=None, ask=None, spread=None,
                              tick_time=None, quality=DataQuality.MISSING)  # fmt: skip
    quality = DataQuality.OK if fresh(q.time, now, QUOTE_MAX_AGE) else DataQuality.STALE
    return MarketSnapshot(symbol=q.symbol, bid=q.bid, ask=q.ask, spread=q.ask - q.bid,
                          tick_time=q.time, quality=quality)  # fmt: skip
