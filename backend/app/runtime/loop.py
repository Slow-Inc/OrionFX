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
It never calls the broker: M06 alone vets and executes intents. It fails closed: when an
input is not known well enough (no Initial Lot, no bars, a stale quote, positions not
freshly reported, a basket outside the open cycle) it decides nothing and reports
DEGRADED; while a cycle is closing it decides nothing. A failing tick reports DOWN and
is not retried until a backoff has passed. Health is journaled on change and as a
heartbeat.
"""

import logging
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.contracts import (
    SNAPSHOT_STALE_AFTER,
    BotPosition,
    CandleClosed,
    CycleUpdated,
    FeatureSnapshot,
    HealthUpdated,
    MarketSnapshot,
    PositionsSnapshot,
    RiskDecision,
    StrategyProposal,
    TradeIntent,
)
from app.core.enums import (
    CycleState,
    DataQuality,
    Direction,
    HealthStatus,
    IntentAction,
    Mode,
    Side,
    Timeframe,
)
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import Clock, fresh, utc_now
from app.decision.context import DecisionContext
from app.decision.decide import decide
from app.decision.fusion import Costs, CycleView, Decision, Evidence, to_events, usable_proposals
from app.decision.gate import Strategy, eligible
from app.decision.manage import Held, manage
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
PENDING_WINDOW = 50
"""Newest intents checked for an M06 decision; intents live seconds, so older ones have
expired (and M06 refuses an expired intent anyway)."""
HEARTBEAT_EVERY = timedelta(seconds=60)
"""An unchanged health status is re-reported this often (a change is reported at once)."""

EvidenceProvider = Callable[[datetime], Evidence]
QuoteProvider = Callable[[datetime], MarketSnapshot]
"""The current quote (M02 `CandleBuilder.snapshot`); price and spread come from it."""

log = logging.getLogger(__name__)


class NotKnown(Exception):
    """An input the decision depends on is not known well enough: no decision this tick."""


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
        quote: QuoteProvider,
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
        self._quote = quote
        self._costs = costs
        self.experiences = experiences
        self._policy = policy
        self._account_id = account_id
        self._bot_id = bot_id
        self._clock = clock
        self._retry_at: datetime | None = None
        self._mode = Mode.SHADOW  # the last mode read; used if reading the config fails
        self._last_health: tuple[HealthStatus, str | None, datetime] | None = None

    # --- M06 state, read from the journal ----------------------------------------------

    def _cycle(self) -> tuple[CycleUpdated | None, str | None, datetime | None]:
        """M06's latest cycle update and its cycle id, and when a cycle last closed (kept
        after the next cycle opens, so the re-entry cooldown still applies)."""
        latest = self._journal.recent(["cycle.updated"], limit=1)
        closed = self._journal.find("cycle.updated", "state", CycleState.CLOSED.value)
        closed_at = None if closed is None else closed.envelope.occurred_at
        if not latest:
            return None, None, closed_at
        return payload_as(latest[0], CycleUpdated), latest[0].envelope.cycle_id, closed_at

    def _positions(self, now: datetime, cycle_id: str | None) -> list[BotPosition]:
        """The bot's positions in the open cycle, from M06's latest snapshot. Raises
        `NotKnown` unless that snapshot is fresh, newer than M06's last risk decision (an
        allowed order may not be reported yet), and describes one open basket only."""
        latest = self._journal.recent(["positions.snapshot"], limit=1)
        if not latest:
            raise NotKnown("bot positions not reported yet")
        snap = payload_as(latest[0], PositionsSnapshot)
        if not fresh(snap.observed_at, now, SNAPSHOT_STALE_AFTER):
            raise NotKnown("bot positions not known (stale)")
        decided = self._journal.recent(["risk.decision"], limit=1)
        if decided and payload_as(decided[0], RiskDecision).decided_at > snap.observed_at:
            raise NotKnown("bot positions not reported since M06's last decision")
        items = list(snap.items)
        if any(p.symbol != self._symbol or p.cycle_id != cycle_id for p in items):
            raise NotKnown("bot positions outside the open cycle")
        if len({p.side for p in items}) > 1:
            raise NotKnown("bot positions on both sides")
        return items

    def _pending(self, now: datetime) -> frozenset[IntentAction]:
        """Intent actions M06 has not decided yet (and that have not expired)."""
        intents = (
            payload_as(ev, TradeIntent)
            for ev in self._journal.unanswered(
                "trade.intent", "risk.decision", "intent_id", within=PENDING_WINDOW
            )
        )
        return frozenset(i.action for i in intents if now < i.valid_until)

    # --- the tick ---------------------------------------------------------------------

    def _health(self, status: HealthStatus, detail: str | None, now: datetime) -> None:
        """Report health when it changes, and re-report it every `HEARTBEAT_EVERY`."""
        last = self._last_health
        if last is not None and last[:2] == (status, detail) and now - last[2] < HEARTBEAT_EVERY:
            return
        self._last_health = (status, detail, now)
        self._journal.append(
            make_event(
                "health.updated", source=SOURCE,
                payload=HealthUpdated(component=COMPONENT, status=status, last_heartbeat=now,
                                      detail=detail),
                account_id=self._account_id, bot_id=self._bot_id, mode=self._mode,
                event_id=stable_id("evt", COMPONENT, now, status), occurred_at=now,
            )
        )  # fmt: skip

    def tick(self) -> Decision | None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return None
        try:
            decision = self._tick(now)
        except NotKnown as unknown:  # refuse to decide on inputs it cannot trust
            self._report(HealthStatus.DEGRADED, str(unknown), now)
            return None
        except Exception as exc:  # a fault reports and backs off; it never loops hot
            self._retry_at = now + BACKOFF
            self._report(HealthStatus.DOWN, f"{type(exc).__name__}: {exc}", now)
            return None
        self._retry_at = None
        return decision

    def _report(self, status: HealthStatus, detail: str, now: datetime) -> None:
        """Health on a failure path: if even the journal fails, log it rather than raise."""
        try:
            self._health(status, detail, now)
        except Exception:
            log.exception("M05 loop could not report %s: %s", status.value, detail)

    def _bars(self, now: datetime) -> dict[Timeframe, list[CandleClosed]]:
        """Every configured timeframe, empty if it has no bar yet, so a missing context
        timeframe makes the feature snapshot MISSING instead of silently vanishing."""
        return {
            tf: self._candles.bars(self._symbol, tf, as_of=now, limit=LOOKBACK)
            for tf in self._timeframes
        }

    def _tick(self, now: datetime) -> Decision | None:
        _, cfg = ConfigStore(self._conn).current()
        mode = self._mode = Mode(cfg.trading.account_mode)
        lot = cfg.trading.initial_lot
        if lot is None:
            raise NotKnown("no Initial Lot configured")
        bars = self._bars(now)
        if not bars[self._timeframes[0]]:
            raise NotKnown("no bars yet")
        quote = self._quote(now)
        if quote.quality is not DataQuality.OK or quote.bid is None:
            raise NotKnown(f"market quote {quote.quality.value.lower()}")
        snapshot = build_feature_snapshot(self._symbol, bars, now)
        ctx = DecisionContext(now=now, mode=mode, snapshot=snapshot, price=quote.bid,
                              spread=quote.spread, initial_lot=lot)  # fmt: skip
        gate = eligible(self._strategies, ctx, enabled=cfg.trading.strategies_enabled,
                        registry=self._registry)  # fmt: skip
        ctx = gate.with_allowed(ctx)
        proposals = [p for s in gate.allowed if (p := s.propose(ctx)) is not None]
        evidence = self._evidence(now)
        upd, cycle_id, closed_at = self._cycle()
        if upd is not None and upd.state is CycleState.CLOSING:
            self._health(HealthStatus.OK, "cycle closing: no new decisions", now)
            return None
        if upd is not None and upd.state is CycleState.CLOSED:
            cycle_id = None
        # what the bot holds decides between entering and managing: always know it
        positions = self._positions(now, cycle_id)
        cycle = CycleView(cycle_id, len(positions), closed_at, self._pending(now))
        net = None if upd is None or cycle_id is None else upd.cycle_net_estimated_usd
        held = _held(positions, net) if positions else None
        pv = self._policy.policy_version
        if held is None:
            d = decide(ctx, proposals, evidence, cycle=cycle, costs=self._costs,
                       symbol=self._symbol, policy_version=pv)  # fmt: skip
        else:
            caps = self._policy.caps_for(cfg.trading.risk_profile)
            d = manage(ctx, proposals, evidence, cycle=cycle, held=held,
                       max_positions=caps.max_positions, costs=self._costs,
                       symbol=self._symbol, policy_version=pv)  # fmt: skip
        self._record(d, snapshot, proposals, held, mode, now)  # LRN-02 before M06 sees it
        for env in to_events(d, account_id=self._account_id, bot_id=self._bot_id):
            self._journal.append(env)
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
