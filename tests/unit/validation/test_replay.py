"""X02 slice B: point-in-time replay harness, cost model and AT-25 scenarios (#67; VAL-02,
VAL-03, AT-15, AT-25). The replay runs the real M02 features and M05 decision code."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

from app.core.bet import LABEL_VERSION
from app.core.contracts import (
    CandleClosed,
    ClassProbabilities,
    Confidence,
    EligibilityRecord,
    PredictionGenerated,
    StrategyManifest,
    StrategyProposal,
)
from app.core.enums import (
    DataQuality,
    DecisionAction,
    Direction,
    Eligibility,
    ForecastLineage,
    IntentAction,
    Mode,
    Side,
    Timeframe,
)
from app.core.ids import new_id
from app.decision.context import DecisionContext
from app.decision.fusion import Evidence
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry
from app.validation.replay import CostModel, replay
from app.validation.scenarios import Scenario, generate

D = Decimal
M15, H1 = Timeframe.M15, Timeframe.H1
T0 = datetime(2026, 1, 5, tzinfo=UTC)
COST = CostModel(contract_size=D(100), commission_per_lot=D("3.5"), slippage=D("0.05"),
                 default_spread=D("0.20"))  # fmt: skip
MANIFEST = StrategyManifest(
    strategy_id="always_up", version="1", artifact_hash="sha256:" + "c" * 64, family="T",
    catalog_ids=["CAT-S01-01"], technique_refs=[], timeframes=[M15], warmup_bars=0,
    parameters={}, risk_tags=[], supported_modes=[Mode.SHADOW, Mode.DEMO],
)  # fmt: skip


@dataclass(frozen=True)
class AlwaysUp:
    """A test strategy: always proposes UP on M15. Not a real rule."""

    manifest: StrategyManifest = MANIFEST
    horizon: Timeframe = M15

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        return StrategyProposal(
            proposal_id=new_id("prop"), strategy_id="always_up", strategy_version="1",
            technique_refs=[], composition_id=None, decision_id=None, direction=Direction.UP,
            action=IntentAction.OPEN, horizon=self.horizon, expected_value_usd=None,
            cost_estimate_usd=None, requested_lot=ctx.initial_lot,
            valid_until=ctx.snapshot.valid_until, rationale="test", constraints=[],
        )  # fmt: skip


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "replay.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def registry(conn: sqlite3.Connection) -> ArtifactRegistry:
    reg = ArtifactRegistry(conn, clock=lambda: T0 - timedelta(days=1))
    reg.register("strategy", MANIFEST, actor="m07")
    reg.set_eligibility(EligibilityRecord(
        subject_hash=MANIFEST.artifact_hash, subject_kind="strategy", subject_ref="always_up@1",
        user_enabled=True, eligibility=Eligibility.SHADOW_ONLY, approved_modes=[Mode.SHADOW],
        context={}, evidence_refs=[], policy_version="p", expires_at=None, revoked_at=None,
    ), actor="owner")  # fmt: skip
    return reg


def _store(conn: sqlite3.Connection, bars: list[CandleClosed]) -> CandleStore:
    store = CandleStore(conn, dataset_version="v1")
    for b in bars:
        store.save(b)
    return store


def _calibrated(now: datetime, _snapshot: object = None, horizon: Timeframe = M15) -> Evidence:
    """A replay evidence provider: it ignores the step's snapshot."""
    return Evidence(forecast=PredictionGenerated(
        prediction_id=f"pred-{now.isoformat()}", model_id="m", model_version="1",
        lineage=ForecastLineage.CHART_ONLY, horizon=horizon, horizon_bars=1, direction=Direction.UP,
        probability=Confidence(value=D("0.8"), semantics="calibrated P(up) over 1 bar",
                               calibrated=True),
        data_quality=DataQuality.OK, input_refs=[], generated_at=now,
        valid_until=now + timedelta(minutes=15),
        class_probabilities=ClassProbabilities(label_version=LABEL_VERSION, up=D("0.8"),
                                               down=D("0.2"), flat=D(0)),
    ))  # fmt: skip


def _run(conn: sqlite3.Connection, registry: ArtifactRegistry, bars: list[CandleClosed],
         **kw: object):  # type: ignore[no-untyped-def]  # fmt: skip
    store = _store(conn, bars)
    args: dict[str, object] = {
        "symbol": "XAUUSD", "timeframe": M15, "context_timeframes": [], "start": T0,
        "end": T0 + timedelta(days=2), "strategies": [AlwaysUp()], "enabled": ["always_up"],
        "registry": registry, "initial_lot": D("0.01"), "cost": COST,
    }  # fmt: skip
    args.update(kw)
    return replay(store, **args)  # type: ignore[arg-type]


TREND = generate(Scenario.STRONG_TREND, start=T0, bars=120)  # ATR warms up over 71 bars


def test_without_a_calibrated_forecast_every_decision_waits_like_the_baseline(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    result = _run(conn, registry, TREND)
    assert result.steps == 120 and len(result.decisions) == 120
    assert {d.action for d in result.decisions} == {DecisionAction.WAIT}
    assert result.trades == [] and result.metrics == result.baseline
    assert result.simplifications  # what this replay ignores is always stated


def test_entries_are_simulated_with_every_cost_against_the_trade(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    result = _run(conn, registry, TREND, evidence=_calibrated)
    assert result.trades and all(t.side is Side.BUY for t in result.trades)
    by_close = {b.available_at: b for b in TREND}
    t = result.trades[0]
    entry_bar, exit_bar = by_close[t.entry_time], by_close[t.exit_time]
    assert t.entry_price == entry_bar.close + D("0.10") + D("0.05")  # half spread + slippage
    assert t.exit_price == exit_bar.close - D("0.10") - D("0.05")
    expected = (t.exit_price - t.entry_price) * D(1) - 2 * D("3.5") * D("0.01")  # 0.01 x 100
    assert t.net_usd == expected.quantize(D("0.01"))
    assert result.metrics["expectancy"].n == len(result.trades)


def test_one_position_at_a_time_then_a_cooldown(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    result = _run(conn, registry, TREND, strategies=[AlwaysUp(horizon=H1)],
                  evidence=partial(_calibrated, horizon=H1))  # fmt: skip
    reasons = [d.reason_codes[0] for d in result.decisions if d.action is DecisionAction.WAIT]
    assert "POSITION_OPEN" in reasons and "COOLDOWN" in reasons
    for a, b in zip(result.trades, result.trades[1:], strict=False):
        assert b.entry_time >= a.exit_time


def test_replay_never_reads_a_bar_before_it_was_available(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    late = CandleClosed(  # an H1 bar for 00:00 that only arrives a day later
        symbol="XAUUSD", timeframe=H1, open_time=T0, close_time=T0 + timedelta(hours=1),
        open=D(9000), high=D(9000), low=D(9000), close=D(9000), volume=None,
        available_at=T0 + timedelta(days=1),
    )  # fmt: skip
    result = _run(conn, registry, [*TREND, late], context_timeframes=[H1])
    steps = [b.available_at for b in TREND]
    assert all(seen <= step for seen, step in zip(result.max_bar_available_at, steps, strict=True))


# --- scenarios -----------------------------------------------------------------------------------


def _closes(kind: Scenario) -> list[Decimal]:
    return [b.close for b in generate(kind, start=T0, bars=60)]


def test_scenarios_are_deterministic() -> None:
    assert generate(Scenario.RANGE, start=T0, bars=30) == generate(
        Scenario.RANGE, start=T0, bars=30
    )
    assert generate(Scenario.RANGE, start=T0, bars=30, seed=1) != generate(
        Scenario.RANGE, start=T0, bars=30, seed=2
    )


def test_scenarios_have_their_shape() -> None:
    trend = _closes(Scenario.STRONG_TREND)
    assert trend[-1] - trend[0] > D(80)
    rng = _closes(Scenario.RANGE)
    assert max(rng) - min(rng) < D(15)
    rev = _closes(Scenario.REVERSAL)
    assert max(rev) - rev[0] > D(40) and max(rev) - rev[-1] > D(40)
    fb = _closes(Scenario.FALSE_BREAKOUT)
    assert max(fb) - fb[0] > D(6) and abs(fb[-1] - fb[0]) < D(8)  # broke out, came back
    spike = generate(Scenario.NEWS_SPIKE, start=T0, bars=60)
    ranges = sorted(b.high - b.low for b in spike)
    assert ranges[-1] > 10 * ranges[len(ranges) // 2]
    assert all(b.spread == D("1.50") for b in generate(Scenario.WIDE_SPREAD, start=T0, bars=5))
    assert all(b.available_at > b.close_time for b in spike)  # known only after the close


def test_a_trade_exits_at_its_horizon_bar_not_one_later(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    result = _run(conn, registry, TREND, evidence=_calibrated)
    t = result.trades[0]
    assert t.exit_time - t.entry_time == timedelta(minutes=15)  # the next M15 bar
    assert not t.forced_exit


def test_a_position_open_at_the_end_is_closed_and_counted(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    h1 = {
        "strategies": [AlwaysUp(horizon=H1)],
        "evidence": partial(_calibrated, horizon=H1),
    }
    first = _run(conn, registry, TREND, **h1).trades[0]
    # end the window two bars after that entry, long before its 1-hour horizon
    entry_open = first.entry_time - timedelta(minutes=15, seconds=1)
    cut = _run(conn, registry, [], end=entry_open + timedelta(minutes=30), **h1)
    last = cut.trades[-1]
    assert last.decision_id == first.decision_id and last.forced_exit
    assert last.exit_time == first.entry_time + timedelta(minutes=15)  # the last bar
    assert cut.metrics["expectancy"].n == len(cut.trades)


def test_the_last_bar_of_the_window_is_replayed(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    result = _run(conn, registry, TREND, end=TREND[-1].open_time + timedelta(minutes=15))
    assert result.steps == len(TREND)


def test_negative_costs_are_refused() -> None:
    with pytest.raises(ValueError):
        CostModel(contract_size=D(100), commission_per_lot=D(0), slippage=D("-0.1"),
                  default_spread=D("0.2"))  # fmt: skip
