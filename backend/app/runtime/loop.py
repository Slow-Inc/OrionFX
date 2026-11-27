"""The runtime decision loop (#84; DEC-01, LRN-02, AT-14, AT-15).

One `tick()` at the clock's `now`:
1. reads the bars available at `now` and builds the real M02 feature snapshot;
2. gathers evidence live at `now` (forecasts, news, reasoning) from the provider;
3. reads M06's state from the journal (open cycle, Cycle Net, the bot's positions from
   `positions.snapshot`, intents M06 has not decided yet);
4. runs the M05 gate and strategies, then `decide` (no position) or `manage` (the bot
   holds a basket), and journals `decision.created` and any `trade.intent` with
   deterministic ids, so a repeated tick on the same inputs adds nothing;
5. records the M07 experience and labels whatever outcomes are now known.
It never calls the broker: M06 alone vets and executes intents. A failing tick reports
`health.updated` (DOWN) and is not retried until a backoff has passed.
"""

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.contracts import (
    BotPosition,
    CandleClosed,
    CycleUpdated,
    FeatureSnapshot,
    HealthUpdated,
    PositionsSnapshot,
    StrategyProposal,
    TradeIntent,
)
from app.core.enums import (
    CycleState,
    Direction,
    HealthStatus,
    IntentAction,
    Mode,
    Side,
    Timeframe,
)
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import Clock, utc_now
from app.decision.context import DecisionContext
from app.decision.decide import decide
from app.decision.fusion import Costs, CycleView, Decision, Evidence, to_events, usable_proposals
from app.decision.gate import Strategy, eligible
from app.decision.manage import Held, manage
from app.engine.engine import SNAPSHOT_EVERY
from app.learning.experience import ExperienceLog
from app.market_data.features import DEFAULT_TECHNIQUES, build_feature_snapshot
from app.market_data.store import CandleStore
from app.risk.policy import DEFAULT_POLICY, RiskPolicy
from app.storage.config_store import ConfigStore
from app.storage.journal import Journal, payload_as
from app.storage.registry import ArtifactRegistry

COMPONENT = "M05.loop"
SOURCE = "m05"
LOOKBACK = max(t.lookback for t in DEFAULT_TECHNIQUES)
BACKOFF = timedelta(seconds=60)
POSITIONS_STALE_AFTER = 2 * SNAPSHOT_EVERY
"""Older than this, M06's positions snapshot no longer says what the bot holds."""
PENDING_LOOKBACK = 50
"""How many recent intents to scan for ones M06 has not decided yet."""

EvidenceProvider = Callable[[datetime], Evidence]


class DecisionLoop:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        candles: CandleStore,
        symbol: str,
        timeframe: Timeframe,
        context_timeframes: Iterable[Timeframe],
        strategies: Sequence[Strategy],
        registry: ArtifactRegistry,
        evidence: EvidenceProvider,
        costs: Costs,
        experiences: ExperienceLog,
        policy: RiskPolicy = DEFAULT_POLICY,
        account_id: str = "local-account",
        bot_id: str = "bot-1",
        clock: Clock = utc_now,
    ) -> None:
        self._conn = conn
        self._journal = Journal(conn)
        self._candles = candles
        self._symbol = symbol
        self._timeframes = list(dict.fromkeys([timeframe, *context_timeframes]))
        self._strategies = list(strategies)
        self._registry = registry
        self._evidence = evidence
        self._costs = costs
        self.experiences = experiences
        self._policy = policy
        self._account_id = account_id
        self._bot_id = bot_id
        self._clock = clock
        self._retry_at: datetime | None = None

    # --- M06 state, read from the journal ----------------------------------------------

    def _cycle(self, now: datetime) -> tuple[CycleView, Decimal | None]:
        latest = self._journal.recent(["cycle.updated"], limit=1)
        pending = self._pending(now)
        if not latest:
            return CycleView(None, 0, None, pending), None
        upd = payload_as(latest[0], CycleUpdated)
        if upd.state is CycleState.CLOSED:
            closed_at = latest[0].envelope.occurred_at
            return CycleView(None, 0, closed_at, pending), None
        return (
            CycleView(latest[0].envelope.cycle_id, 0, None, pending),
            upd.cycle_net_estimated_usd,
        )

    def _positions(self, now: datetime) -> list[BotPosition] | None:
        """The bot's positions from M06's latest snapshot, or None when not known at `now`
        (no snapshot, or one older than `POSITIONS_STALE_AFTER`)."""
        latest = self._journal.recent(["positions.snapshot"], limit=1)
        if not latest:
            return None
        snap = payload_as(latest[0], PositionsSnapshot)
        if now - snap.observed_at > POSITIONS_STALE_AFTER:
            return None
        return list(snap.items)

    def _pending(self, now: datetime) -> frozenset[IntentAction]:
        """Intent actions M06 has not decided yet (and that have not expired)."""
        out: set[IntentAction] = set()
        for ev in self._journal.recent(["trade.intent"], limit=PENDING_LOOKBACK):
            intent = payload_as(ev, TradeIntent)
            if intent.valid_until <= now:
                continue
            if self._journal.find("risk.decision", "intent_id", intent.intent_id) is None:
                out.add(intent.action)
        return frozenset(out)

    # --- the tick ---------------------------------------------------------------------

    def _health(self, status: HealthStatus, detail: str | None, now: datetime) -> None:
        self._journal.append(
            make_event(
                "health.updated", source=SOURCE,
                payload=HealthUpdated(component=COMPONENT, status=status, last_heartbeat=now,
                                      detail=detail),
                account_id=self._account_id, bot_id=self._bot_id, mode=self._mode(),
                event_id=stable_id("evt", COMPONENT, now, status), occurred_at=now,
            )
        )  # fmt: skip

    def _mode(self) -> Mode:
        _, cfg = ConfigStore(self._conn).current()
        return Mode(cfg.trading.account_mode)

    def tick(self) -> Decision | None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return None
        try:
            decision = self._tick(now)
        except Exception as exc:  # a fault reports and backs off; it never loops hot
            self._retry_at = now + BACKOFF
            self._health(HealthStatus.DOWN, f"{type(exc).__name__}: {exc}", now)
            return None
        self._retry_at = None
        return decision

    def _bars(self, now: datetime) -> dict[Timeframe, list[CandleClosed]]:
        out: dict[Timeframe, list[CandleClosed]] = {}
        for tf in self._timeframes:
            bars = self._candles.bars(self._symbol, tf, as_of=now, limit=LOOKBACK)
            if bars:
                out[tf] = bars
        return out

    def _tick(self, now: datetime) -> Decision | None:
        _, cfg = ConfigStore(self._conn).current()
        lot = cfg.trading.initial_lot
        bars = self._bars(now)
        if lot is None or self._timeframes[0] not in bars:
            self._health(HealthStatus.DEGRADED, "no initial lot or no bars yet", now)
            return None
        last = bars[self._timeframes[0]][-1]  # the store returns only bars available at now
        snapshot = build_feature_snapshot(self._symbol, bars, now)
        mode = Mode(cfg.trading.account_mode)
        ctx = DecisionContext(now=now, mode=mode, snapshot=snapshot, price=last.close,
                              spread=last.spread, initial_lot=lot)  # fmt: skip
        gate = eligible(self._strategies, ctx, enabled=cfg.trading.strategies_enabled,
                        registry=self._registry)  # fmt: skip
        ctx = gate.with_allowed(ctx)
        proposals = [p for s in gate.allowed if (p := s.propose(ctx)) is not None]
        evidence = self._evidence(now)
        cycle, cycle_net = self._cycle(now)
        positions: list[BotPosition] = []
        if cycle.cycle_id is not None:  # entering or managing depends on what the bot holds
            known = self._positions(now)
            if known is None:
                self._health(HealthStatus.DEGRADED, "bot positions not known (stale)", now)
                return None
            positions = known
            cycle = replace(cycle, open_positions=len(positions))
        held = _held(positions, cycle_net) if positions else None
        pv = self._policy.policy_version
        if held is None:
            d = decide(ctx, proposals, evidence, cycle=cycle, costs=self._costs,
                       symbol=self._symbol, policy_version=pv)  # fmt: skip
        else:
            caps = self._policy.caps_for(cfg.trading.risk_profile)
            d = manage(ctx, proposals, evidence, cycle=cycle, held=held,
                       max_positions=caps.max_positions, costs=self._costs,
                       symbol=self._symbol, policy_version=pv)  # fmt: skip
        for env in to_events(d, account_id=self._account_id, bot_id=self._bot_id):
            self._journal.append(env)
        self._record(d, snapshot, proposals, held, mode, now)
        self.experiences.label_due(now=now)
        self._health(HealthStatus.OK, None, now)
        return d

    def _record(self, d: Decision, snapshot: FeatureSnapshot, proposals: list[StrategyProposal],
                held: Held | None, mode: Mode, now: datetime) -> None:  # fmt: skip
        chosen = usable_proposals(proposals, now)
        horizon = chosen[0].horizon if chosen else self._timeframes[0]
        direction: Direction | None = chosen[0].direction if chosen else None
        if held is not None:
            direction = Direction.UP if held.side is Side.BUY else Direction.DOWN
        self.experiences.record(
            decision=d.created, snapshot=snapshot, mode=mode, symbol=self._symbol,
            cycle_id=d.cycle_id, intent_id=d.intent.intent_id if d.intent else None,
            direction=direction, horizon=horizon, horizon_bars=1,
        )  # fmt: skip


def _held(positions: list[BotPosition], cycle_net: Decimal | None) -> Held:
    return Held(side=positions[0].side, positions=len(positions), cycle_net_usd=cycle_net,
                position_refs=[p.position_id for p in positions])  # fmt: skip
