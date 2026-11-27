"""M05 slice A: strategy port, registry gate and MVP rule strategies (#52; DEC-03,
UST-01/03/06/08; AT-28, AT-32). Strategies are pure rules over the M02 snapshot."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.contracts import EligibilityRecord, FeatureSnapshot
from app.core.enums import DataQuality, Direction, Eligibility, Mode, Timeframe
from app.decision.context import DecisionContext
from app.decision.gate import eligible
from app.decision.strategies import STRATEGIES, catalog_ids
from app.market_data.features import feature_key
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
H1, M15, M5 = Timeframe.H1, Timeframe.M15, Timeframe.M5


def k(tf: Timeframe, cat: str, out: str) -> str:
    return feature_key(tf, cat, out)


BASE = {
    k(H1, "CAT-T03-07", "direction"): D(1),
    k(M15, "CAT-T03-07", "direction"): D(1),
    k(M5, "CAT-T03-07", "direction"): D(1),
    k(M15, "CAT-T03-04", "structure"): D(1),
    k(H1, "CAT-T01-07", "adx"): D(30),
    k(M15, "CAT-T03-06", "trend"): D(1),
    k(M15, "CAT-T03-06", "range"): D(0),
    k(M5, "CAT-T01-04", "rsi"): D(55),
    k(M15, "CAT-T01-06", "atr"): D(4),
    k(M15, "CAT-T03-03", "support"): D(1990),
    k(M15, "CAT-T03-03", "resistance"): D(2010),
}


def _ctx(
    price: str = "2000", spread: str = "0.20", mode: Mode = Mode.DEMO, **values: Decimal | None
) -> DecisionContext:
    snap = FeatureSnapshot(
        symbol="XAUUSD",
        feature_schema="m02.features.v2",
        values={**BASE, **values},
        quality=DataQuality.OK,
        as_of=T0,
        available_at=T0 + timedelta(seconds=1),
        valid_until=T0 + timedelta(minutes=5),
    )
    return DecisionContext(
        now=NOW, mode=mode, snapshot=snap, price=D(price), spread=D(spread), initial_lot=D("0.01")
    )


def _side(strategy_id: str, ctx: DecisionContext) -> Direction | None:
    p = STRATEGIES[strategy_id].propose(ctx)
    return None if p is None else p.direction


# --- rules -------------------------------------------------------------------------------------


def test_trend_following_needs_aligned_structure_and_a_trending_adx() -> None:
    assert _side("trend_following", _ctx()) is Direction.UP
    assert _side("trend_following", _ctx(**{k(H1, "CAT-T01-07", "adx"): D(15)})) is None
    assert _side("trend_following", _ctx(**{k(M15, "CAT-T03-04", "structure"): D(-1)})) is None


def test_breakout_fires_beyond_a_level_outside_a_range() -> None:
    assert _side("breakout", _ctx(price="2012")) is Direction.UP
    assert _side("breakout", _ctx(price="1987")) is Direction.DOWN
    assert _side("breakout", _ctx(price="2000")) is None
    ranging = {k(M15, "CAT-T03-06", "range"): D(1)}
    assert _side("breakout", _ctx(price="2012", **ranging)) is None


def test_pullback_enters_with_the_trend_after_a_counter_move() -> None:
    assert _side("pullback", _ctx(**{k(M5, "CAT-T01-04", "rsi"): D(35)})) is Direction.UP
    assert _side("pullback", _ctx()) is None  # no pullback (RSI 55)
    down = {k(H1, "CAT-T03-07", "direction"): D(-1), k(M5, "CAT-T01-04", "rsi"): D(65)}
    assert _side("pullback", _ctx(**down)) is Direction.DOWN


def test_range_fades_the_edges_only_in_a_range_regime() -> None:
    ranging = {k(M15, "CAT-T03-06", "range"): D(1), k(M15, "CAT-T03-06", "trend"): D(0)}
    assert _side("range", _ctx(price="1990.5", **ranging)) is Direction.UP
    assert _side("range", _ctx(price="2009.5", **ranging)) is Direction.DOWN
    assert _side("range", _ctx(price="2000", **ranging)) is None
    assert _side("range", _ctx(price="1990.5")) is None  # trending, not a range
    assert _side("range", _ctx(price="1975", **ranging)) is None  # breakdown, not support
    assert _side("range", _ctx(price="2025", **ranging)) is None  # breakout, not resistance


def test_scalping_needs_aligned_fast_timeframes_and_a_tight_spread() -> None:
    p = STRATEGIES["scalping"].propose(_ctx())
    assert p is not None and p.direction is Direction.UP and p.horizon is M5
    assert _side("scalping", _ctx(spread="0.60")) is None
    assert _side("scalping", _ctx(**{k(M5, "CAT-T03-07", "direction"): D(-1)})) is None


ALL = frozenset(s for s in STRATEGIES if s != "hybrid")


def test_hybrid_needs_two_allowed_strategies_to_agree() -> None:
    assert _side("hybrid", replace(_ctx(), allowed=ALL)) is Direction.UP  # trend + scalping
    weak = {k(H1, "CAT-T01-07", "adx"): D(10), k(M5, "CAT-T03-07", "direction"): D(-1)}
    assert _side("hybrid", replace(_ctx(**weak), allowed=ALL)) is None


def test_hybrid_ignores_members_the_gate_did_not_allow() -> None:
    assert _side("hybrid", _ctx()) is None  # nothing allowed: no votes
    assert _side("hybrid", replace(_ctx(), allowed=frozenset({"trend_following"}))) is None


def test_missing_feature_means_no_proposal() -> None:
    assert _side("trend_following", _ctx(**{k(H1, "CAT-T01-07", "adx"): None})) is None
    no_resistance = {k(M15, "CAT-T03-03", "resistance"): None}
    assert _side("breakout", _ctx(price="2012", **no_resistance)) is None  # nothing to break
    assert _side("breakout", _ctx(price="1980", **no_resistance)) is Direction.DOWN


def test_proposals_are_typed_sized_with_the_initial_lot_and_cite_their_features() -> None:
    p = STRATEGIES["trend_following"].propose(_ctx())
    assert p is not None
    assert p.requested_lot == D("0.01") and p.strategy_version == "1.0.0"
    assert k(H1, "CAT-T01-07", "adx") in p.technique_refs
    assert p.valid_until == T0 + timedelta(minutes=5)


def test_every_mvp_rule_strategy_maps_to_its_catalog_row() -> None:
    assert catalog_ids() == {f"CAT-S01-0{i}" for i in range(1, 7)}


# --- gate (AT-32) ------------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[ArtifactRegistry]:
    c = connect(tmp_path / "gate.db")
    migrate(c)
    reg = ArtifactRegistry(c, clock=lambda: T0 - timedelta(hours=1))
    for s in STRATEGIES.values():
        reg.register("strategy", s.manifest, actor="m07")
    yield reg
    c.close()


def _approve(reg: ArtifactRegistry, sid: str, modes: list[Mode], **over: object) -> None:
    m = STRATEGIES[sid].manifest
    record: dict[str, object] = {
        "subject_hash": m.artifact_hash, "subject_kind": "strategy",
        "subject_ref": f"{m.strategy_id}@{m.version}", "user_enabled": True,
        "eligibility": Eligibility.DEMO_ELIGIBLE, "approved_modes": modes, "context": {},
        "evidence_refs": ["x02"], "policy_version": "p", "expires_at": None, "revoked_at": None,
    }  # fmt: skip
    record.update(over)
    reg.set_eligibility(EligibilityRecord.model_validate(record), actor="owner")


def test_only_enabled_and_approved_strategies_pass(registry: ArtifactRegistry) -> None:
    _approve(registry, "trend_following", [Mode.SHADOW, Mode.DEMO])
    _approve(registry, "breakout", [Mode.SHADOW, Mode.DEMO])
    result = eligible(
        STRATEGIES.values(), _ctx(), enabled=["trend_following", "scalping"], registry=registry
    )
    assert [s.manifest.strategy_id for s in result.allowed] == ["trend_following"]
    assert result.refused["scalping"] == "not approved for DEMO"
    assert result.refused["breakout"] == "not enabled by the owner"


def test_unusable_snapshot_refuses_everything(registry: ArtifactRegistry) -> None:
    _approve(registry, "trend_following", [Mode.DEMO])
    base = _ctx()
    stale = replace(base, snapshot=base.snapshot.model_copy(update={"quality": DataQuality.STALE}))
    result = eligible(STRATEGIES.values(), stale, enabled=["trend_following"], registry=registry)
    assert result.allowed == [] and "snapshot" in result.refused["trend_following"]


def test_expired_approval_is_judged_at_now(registry: ArtifactRegistry) -> None:
    _approve(registry, "trend_following", [Mode.DEMO], expires_at=datetime(2099, 1, 1, tzinfo=UTC))
    ok = eligible(STRATEGIES.values(), _ctx(), enabled=["trend_following"], registry=registry)
    assert len(ok.allowed) == 1
    late = replace(_ctx(), now=datetime(2099, 1, 2, tzinfo=UTC))
    result = eligible(STRATEGIES.values(), late, enabled=["trend_following"], registry=registry)
    assert result.refused["trend_following"] == "not approved for DEMO"


def test_approval_for_another_mode_does_not_count(registry: ArtifactRegistry) -> None:
    _approve(registry, "trend_following", [Mode.SHADOW], eligibility=Eligibility.SHADOW_ONLY)
    result = eligible(STRATEGIES.values(), _ctx(), enabled=["trend_following"], registry=registry)
    assert result.refused["trend_following"] == "not approved for DEMO"


@pytest.mark.parametrize("sid", sorted(STRATEGIES))
def test_each_strategy_passes_the_gate_only_when_enabled_and_approved(
    registry: ArtifactRegistry, sid: str
) -> None:
    assert eligible(STRATEGIES.values(), _ctx(), enabled=[sid], registry=registry).allowed == []
    _approve(registry, sid, [Mode.DEMO])
    result = eligible(STRATEGIES.values(), _ctx(), enabled=[sid], registry=registry)
    assert [s.manifest.strategy_id for s in result.allowed] == [sid]


def test_an_approval_for_other_code_is_refused(registry: ArtifactRegistry) -> None:
    _approve(registry, "trend_following", [Mode.DEMO])
    changed = replace(
        STRATEGIES["trend_following"],
        manifest=STRATEGIES["trend_following"].manifest.model_copy(
            update={"artifact_hash": "sha256:" + "f" * 64}
        ),
    )
    result = eligible([changed], _ctx(), enabled=["trend_following"], registry=registry)
    assert "hash" in result.refused["trend_following"]


def test_an_approval_recorded_after_now_does_not_count(tmp_path: Path) -> None:
    c = connect(tmp_path / "late.db")
    migrate(c)
    reg = ArtifactRegistry(c, clock=lambda: T0 + timedelta(days=30))  # recorded in the future
    reg.register("strategy", STRATEGIES["trend_following"].manifest, actor="m07")
    _approve(reg, "trend_following", [Mode.DEMO])
    result = eligible(STRATEGIES.values(), _ctx(), enabled=["trend_following"], registry=reg)
    assert result.refused["trend_following"] == "not approved for DEMO"
    c.close()
