"""The autonomous champion/candidate loop (#133; ADR 0001 with ADR 0002; protocol v1 §1a,
§8.4, §9): admission to shadow, the once-only judgement, promotion, the kill switches,
renewal, and the hard guards.

The claim ledger and the artifact store are fakes behind the loop's ports; scores come
from a fake resolver (the real one reads `prediction_labels`, tested below with a stubbed
source). Everything else is the real lifecycle and registry on a migrated database."""

import ast
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.learning.autonomy as autonomy_module
import app.runtime.autonomy as runtime_module
from app.core.bet import LABEL_VERSION
from app.core.contracts import ClassProbabilities, FeatureSnapshot, sha256_ref
from app.core.enums import DataQuality, Direction, Eligibility, ForecastLineage, Mode, Timeframe
from app.learning.autonomy import (
    Action,
    Autonomy,
    AutonomyRefused,
    CertifiedProtocol,
    Check,
    Claim,
    ModelFiles,
    PromotionPolicy,
    Scored,
    breaches,
    label_resolver,
)
from app.learning.bars import BarLog, Provenance, RecordedPrediction
from app.learning.labels import Outcome, TripleBarrierLabel
from app.learning.lifecycle import (
    OWNER_ACTOR,
    SYSTEM_ACTOR,
    DatasetManifest,
    DatasetStore,
    ModelLifecycle,
    Split,
)
from app.market_data.features import FEATURE_SCHEMA
from app.market_data.store import CandleStore
from app.prediction.card import ModelCard, TrainingRecord
from app.prediction.transforms import TRANSFORM_SCHEMA
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

D = Decimal
T0 = datetime(2027, 1, 4, tzinfo=UTC)
DAY = timedelta(days=1)
HASH = "a" * 64
OTHER_HASH = "b" * 64
CONFIG = "sha256:" + "c" * 64
LIVE_REF = "x02:live-qualification:owner-issued"
"""Stands in for an X02-verified Live qualification the owner holds."""
MODEL = "m04-xau"
VALUES = {
    "autonomy.label_version": LABEL_VERSION,
    "autonomy.feature_schema": TRANSFORM_SCHEMA,
    "autonomy.modes": "SHADOW,DEMO",
    "autonomy.shadow_min_days": "90",
    "autonomy.shadow_max_days": "120",
    "autonomy.shadow_min_resolved": "300",
    "autonomy.min_brier_gain": "0.002",
    "autonomy.cusum_k": "0.05",
    "autonomy.cusum_h": "5",
    "autonomy.ece_max": "0.08",
    "autonomy.ece_min_resolved": "300",
    "autonomy.psi_max": "0.25",
    "autonomy.psi_window_days": "5",
    "autonomy.psi_min_rows": "50",
    "autonomy.demo_expectancy_floor_usd": "0",
    "autonomy.demo_min_cycles": "30",
    "autonomy.approval_ttl_hours": "4800",
    "autonomy.renew_within_hours": "24",
}
"""Test numbers only: the real ones are the certified protocol's (training-loop.md §7)."""


# --- fakes behind the ports -------------------------------------------------------------------


@dataclass
class Ledger:
    protocols: dict[str, CertifiedProtocol] = field(
        default_factory=lambda: {
            HASH: CertifiedProtocol("agent under owner delegation, 2026-11-28", VALUES)
        }
    )
    consumed: set[str] = field(default_factory=lambda: {HASH})
    claims: dict[str, Claim] = field(
        default_factory=lambda: {
            HASH: Claim(CONFIG, True, True, True, ("x02:run:wf", "x02:run:holdout"))
        }
    )
    parity: dict[tuple[str, str], Check] = field(default_factory=dict)

    def certified(self, protocol_hash: str) -> CertifiedProtocol | None:
        return self.protocols.get(protocol_hash)

    def holdout_consumed(self, protocol_hash: str) -> bool:
        return protocol_hash in self.consumed

    def claim(self, protocol_hash: str) -> Claim | None:
        return self.claims.get(protocol_hash)

    def spot_parity(self, protocol_hash: str, model_ref: str) -> Check | None:
        return self.parity.get((protocol_hash, model_ref))  # fmt: skip


@dataclass
class Artifacts:
    files_: dict[str, ModelFiles] = field(default_factory=dict)

    def files(self, model_id: str, version: str) -> ModelFiles | None:
        return self.files_.get(f"{model_id}@{version}")


@dataclass
class Scores:
    """The fake resolver: per version, (bar, probabilities, label) rows."""

    rows: dict[str, Scored] = field(default_factory=dict)

    def __call__(self, model_id: str, version: str, start: datetime, now: datetime) -> Scored:
        return {t: v for t, v in self.rows.get(version, {}).items() if start <= t < now}


def _card(version: str, **over: Any) -> ModelCard:
    weights = f"weights-{version}".encode()
    base: dict[str, Any] = {
        "model_id": MODEL, "model_version": version,
        "artifact_hash": "sha256:" + "d" * 64, "lineage": ForecastLineage.CHART_ONLY,
        "feature_schema": TRANSFORM_SCHEMA, "required_features": ["H1.close_sma_atr"],
        "feature_ranges": {}, "horizon": Timeframe.H1, "horizon_bars": 1,
        "semantics": "P(UP, DOWN, FLAT) of tb.v2", "calibrated": True,
        "validation": {"brier": D("0.60")}, "evidence_refs": ["x02:run:wf"],
        "label_version": LABEL_VERSION, "training_cutoff": T0 - 30 * DAY,
        "weights_hash": sha256_ref(weights),
        "training": TrainingRecord(dataset_ref="gc@1", config_hash=CONFIG, seeds=[1],
                                   libraries={"lightgbm": "4.7.0"}, code_commit="abc1234"),
    }  # fmt: skip
    base.update(over)
    return ModelCard.model_validate(base)


class World:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.now = T0
        self.registry = ArtifactRegistry(conn, clock=lambda: self.now)
        self.life = ModelLifecycle(conn, self.registry, DatasetStore(conn),
                                   clock=lambda: self.now,
                                   live_verifier=lambda ref, _: ref == LIVE_REF)  # fmt: skip
        self.life.datasets.save(DatasetManifest(
            dataset_id="gc", version="1", source_refs=["gc:firstrate"], window_start=T0 - 900 * DAY,
            window_end=T0 - 10 * DAY, splits=[
                Split(name="train", start=T0 - 900 * DAY, end=T0 - 100 * DAY),
                Split(name="validation", start=T0 - 100 * DAY, end=T0 - 10 * DAY)],
            content_hash="sha256:" + "e" * 64))  # fmt: skip
        self.ledger = Ledger()
        self.artifacts = Artifacts()
        self.scores = Scores()
        self.nets: list[Decimal] = []
        self.candles = CandleStore(conn, dataset_version="v1")
        self.loop = self.autonomy(HASH)

    def autonomy(self, protocol_hash: str) -> Autonomy:
        return Autonomy(self.conn, lifecycle=self.life, ledger=self.ledger,
                        artifacts=self.artifacts, candles=self.candles, symbol="XAUUSD",
                        timeframe=Timeframe.M15, model_id=MODEL, protocol_hash=protocol_hash,
                        resolver=self.scores, demo_nets=lambda since: self.nets,
                        clock=lambda: self.now)  # fmt: skip

    def candidate(self, version: str, *, parity: bool = True, **over: Any) -> ModelCard:
        card = _card(version, **over)
        self.life.propose_candidate(card, dataset_refs=["gc@1"])
        self.artifacts.files_[card.ref] = ModelFiles(card, b"w", "app.prediction.adapters.x")
        self.ledger.parity[(HASH, card.ref)] = Check(parity, f"x02:run:parity-{version}")
        return card

    def champion(self, version: str, **over: Any) -> None:
        """A champion the owner promoted, in SHADOW and DEMO, before T0."""
        self.candidate(version, **over)
        self.life.promote(MODEL, version, modes=[Mode.SHADOW, Mode.DEMO],
                          evidence_refs=["x02:run:old"], policy_ref="p", approved_by=OWNER_ACTOR,
                          approval_ref="owner")  # fmt: skip

    def predict(self, version: str, start: datetime, n: int, q: float | None) -> None:
        """`n` hourly LIVE predictions with resolved labels (`_row`)."""
        rows = self.scores.rows.setdefault(version, {})
        for i in range(n):
            t = start + i * HOUR
            rows[t] = _row(t, q)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "autonomy.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def w(conn: sqlite3.Connection) -> World:
    return World(conn)


HOUR = timedelta(hours=1)
PATTERN = (0, 0, 0, 0, 0, 1, 1, 1, 2, 2)
"""The label by the hour: 50/30/20 UP/DOWN/FLAT, the same for every model on a bar."""
CLIMATE = None
"""Climatology (0.5, 0.3, 0.2): calibrated, Brier 0.62."""
SHARP, SHARPER = 0.9, 0.95
"""q on the true class, the rest split: Brier 0.015 and 0.00375, ECE below 0.08."""
TTL = timedelta(hours=4800)


def _row(t: datetime, q: float | None) -> tuple[tuple[float, float, float], int]:
    y = PATTERN[int((t - T0) / HOUR) % len(PATTERN)]
    if q is None:
        return (0.5, 0.3, 0.2), y
    p = [(1 - q) / 2] * 3
    p[y] = q
    return (p[0], p[1], p[2]), y


# --- the policy comes from the certified protocol ---------------------------------------------


def test_every_number_comes_from_the_certified_protocol() -> None:
    policy = PromotionPolicy.from_protocol(HASH, Ledger().certified(HASH))
    assert policy.modes == (Mode.SHADOW, Mode.DEMO)
    assert policy.shadow_min_resolved == 300 and policy.cusum_h == D(5)
    assert policy.ttl == TTL and policy.ref == f"protocol:{HASH}"


@pytest.mark.parametrize("name", ["autonomy.cusum_h", "autonomy.shadow_min_days"])
@pytest.mark.parametrize("value", [None, "", "five", "-1", "NaN"])
def test_a_missing_or_unparseable_number_refuses_the_whole_policy(
    name: str, value: str | None
) -> None:
    values = {k: v for k, v in VALUES.items() if k != name}
    if value is not None:
        values[name] = value
    with pytest.raises(AutonomyRefused, match=f"POLICY_INCOMPLETE:{name}"):
        PromotionPolicy.from_protocol(HASH, CertifiedProtocol("agent", values))


@pytest.mark.parametrize("modes", ["SHADOW,DEMO,LIVE", "LIVE", "DEMO,live"])
def test_the_policy_never_names_live(modes: str) -> None:
    with pytest.raises(AutonomyRefused, match="POLICY"):
        PromotionPolicy.from_protocol(HASH, CertifiedProtocol(
            "agent", {**VALUES, "autonomy.modes": modes}))  # fmt: skip


def test_an_unhashed_or_uncertified_protocol_refuses_everything(w: World) -> None:
    """Guard: a candidate whose protocol is not registered under a hash is refused."""
    w.candidate("2")
    with pytest.raises(AutonomyRefused, match="PROTOCOL_UNREGISTERED"):
        w.autonomy(OTHER_HASH).submit("2", protocol_hash=OTHER_HASH, mode=Mode.DEMO)
    w.ledger.protocols[HASH] = CertifiedProtocol(" ", VALUES)
    with pytest.raises(AutonomyRefused, match="PROTOCOL_UNCERTIFIED"):
        w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO)
    with pytest.raises(AutonomyRefused):
        w.loop.step(mode=Mode.DEMO)
    assert w.loop.history() == []


# --- admission to shadow -----------------------------------------------------------------------


def test_a_complete_candidate_shadows_for_shadow_only_and_is_recorded(w: World) -> None:
    w.champion("1")
    w.candidate("2")
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == []
    rec = w.registry.eligibility("model", f"{MODEL}@2")
    assert rec is not None and rec.approved_modes == [Mode.SHADOW]
    assert rec.expires_at == T0 + 120 * DAY  # the window's end
    assert w.life.champion(MODEL) == "1"
    [(at, admitted)] = w.loop.history("2")
    assert at == T0 and admitted.action is Action.ADMIT and admitted.actor == SYSTEM_ACTOR
    assert admitted.protocol_hash == HASH
    assert admitted.evidence_refs == ["x02:run:wf", "x02:run:holdout", "x02:run:parity-2"]
    assert [s.version for s in w.loop.shadowing()] == ["2"]


def test_a_candidate_whose_holdout_is_not_consumed_is_refused(w: World) -> None:
    """Guard: the holdout must have been consumed (opened, once) under the protocol."""
    w.candidate("2")
    w.ledger.consumed.clear()
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == ["HOLDOUT_NOT_CONSUMED"]
    assert w.registry.eligibility("model", f"{MODEL}@2") is None
    assert w.loop.history("2")[0][1].action is Action.REFUSE
    assert w.loop.shadowing() == []


@pytest.mark.parametrize(("claim", "reason"), [
    (Claim(CONFIG, False, True, True, ()), "MODEL_TIER_NOT_PASSED"),
    (Claim(CONFIG, True, False, True, ()), "SYSTEM_TIER_NOT_PASSED"),
    (Claim(CONFIG, True, True, False, ()), "HOLDOUT_NOT_PASSED"),
    (Claim("sha256:" + "f" * 64, True, True, True, ()), "CLAIM_OF_ANOTHER_CONFIG"),
    (None, "NO_CLAIM"),
])  # fmt: skip
def test_the_full_evidence_set_is_required(w: World, claim: Claim | None, reason: str) -> None:
    w.candidate("2")
    if claim is None:
        del w.ledger.claims[HASH]
    else:
        w.ledger.claims[HASH] = claim
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == [reason]


def test_spot_parity_must_have_passed(w: World) -> None:
    w.candidate("2", parity=False)
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == ["SPOT_PARITY_NOT_PASSED"]
    w.candidate("3")
    del w.ledger.parity[(HASH, f"{MODEL}@3")]
    assert w.loop.submit("3", protocol_hash=HASH, mode=Mode.DEMO) == ["SPOT_PARITY_NOT_PASSED"]


@pytest.mark.parametrize(("over", "reason"), [
    ({"label_version": None}, "LABEL_VERSION_MISMATCH"),
    ({"label_version": "m07.labels.tb.v1"}, "LABEL_VERSION_MISMATCH"),
    ({"feature_schema": FEATURE_SCHEMA}, "FEATURE_SCHEMA_MISMATCH"),
])  # fmt: skip
def test_a_label_version_or_feature_schema_mismatch_is_refused(
    w: World, over: dict[str, Any], reason: str
) -> None:
    w.candidate("2", **over)
    assert reason in w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO)
    assert w.loop.shadowing() == []


def test_the_policy_label_version_must_be_the_current_one(w: World) -> None:
    w.ledger.protocols[HASH] = CertifiedProtocol("agent", {
        **VALUES, "autonomy.label_version": "m07.labels.tb.v9"})  # fmt: skip
    w.candidate("2", label_version="m07.labels.tb.v9")
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == ["LABEL_VERSION_MISMATCH"]


def test_an_edited_or_unregistered_card_is_refused(w: World) -> None:
    card = w.candidate("2")
    edited = card.model_copy(update={"calibrated": True, "validation": {"brier": D("0.1")}})
    w.artifacts.files_[card.ref] = ModelFiles(edited, b"w", "m")
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == ["CARD_NOT_REGISTERED"]


def test_a_version_is_submitted_once_ever(w: World) -> None:
    w.candidate("2")
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == []
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == ["ALREADY_SUBMITTED"]


@pytest.mark.parametrize("call", ["submit", "step"])
def test_a_live_account_mode_refuses_the_loop(w: World, call: str) -> None:
    """Guard: the loop acts in SHADOW and DEMO only. `TradingConfig` cannot say LIVE
    today, so the mode is passed in, and the guard is the loop's own."""
    w.candidate("2")
    with pytest.raises(AutonomyRefused, match="SHADOW and DEMO only"):
        if call == "submit":
            w.loop.submit("2", protocol_hash=HASH, mode=Mode.LIVE)
        else:
            w.loop.step(mode=Mode.LIVE)
    assert w.loop.history() == [] and w.registry.eligibility("model", f"{MODEL}@2") is None


# --- judging, once ----------------------------------------------------------------------------


def _admitted(w: World, version: str = "2") -> None:
    w.candidate(version)
    assert w.loop.submit(version, protocol_hash=HASH, mode=Mode.DEMO) == []


def test_a_candidate_that_beats_the_champion_is_promoted_after_the_window(w: World) -> None:
    w.champion("1")
    _admitted(w)
    w.predict("1", T0, 400, CLIMATE)
    w.predict("2", T0, 400, SHARP)
    w.now = T0 + 30 * DAY
    assert w.loop.step(mode=Mode.DEMO) == []  # the window has not passed: no peeking
    w.now = T0 + 90 * DAY
    [promoted] = w.loop.step(mode=Mode.DEMO)
    assert promoted.action is Action.PROMOTE and promoted.actor == SYSTEM_ACTOR
    assert (promoted.champion_before, promoted.champion_after) == ("1", "2")
    assert promoted.protocol_hash == HASH and promoted.evidence_refs
    assert promoted.approval_before is not None and promoted.approval_after is not None
    assert promoted.approval_after.approved_modes == [Mode.SHADOW, Mode.DEMO]
    assert promoted.approval_after.expires_at == w.now + TTL
    assert promoted.approval_after.policy_version == f"protocol:{HASH}"
    assert promoted.metrics["n"] == "400" and promoted.metrics["reference"] == f"champion:{MODEL}@1"
    assert w.life.champion(MODEL) == "2"
    old = w.registry.eligibility("model", f"{MODEL}@1")
    assert old is not None and old.eligibility is Eligibility.DISABLED
    assert w.loop.step(mode=Mode.DEMO) == []  # judged once: nothing left to do


def test_with_no_champion_the_candidate_must_beat_climatology(w: World) -> None:
    _admitted(w)
    w.predict("2", T0, 400, SHARP)
    w.now = T0 + 91 * DAY
    [promoted] = w.loop.step(mode=Mode.SHADOW)
    assert promoted.action is Action.PROMOTE and promoted.metrics["reference"] == "climatology"


def test_a_candidate_that_does_not_beat_the_champion_is_rejected_for_good(w: World) -> None:
    w.champion("1")
    _admitted(w)
    w.predict("1", T0, 400, SHARP)
    w.predict("2", T0, 400, SHARP)  # equal: no gain
    w.now = T0 + 90 * DAY
    [rejected] = w.loop.step(mode=Mode.DEMO)
    assert rejected.action is Action.REJECT
    assert rejected.reasons == ["DOES_NOT_BEAT_THE_CHAMPION"]
    rec = w.registry.eligibility("model", f"{MODEL}@2")
    assert rec is not None and rec.eligibility is Eligibility.DISABLED
    w.predict("2", T0 + 400 * HOUR, 600, SHARPER)
    w.now = T0 + 100 * DAY
    assert w.loop.step(mode=Mode.DEMO) == []  # never re-judged, however it does later
    assert w.life.champion(MODEL) == "1"


def test_a_window_without_enough_labels_expires(w: World) -> None:
    _admitted(w)
    w.predict("2", T0, 100, SHARP)
    w.now = T0 + 119 * DAY
    assert w.loop.step(mode=Mode.DEMO) == []
    w.now = T0 + 120 * DAY
    [expired] = w.loop.step(mode=Mode.DEMO)
    assert expired.action is Action.EXPIRE and w.life.champion(MODEL) is None


def test_a_changed_protocol_refuses_the_promotion(w: World) -> None:
    """Guard (ADR 0001): no promotion under a policy other than the one it was admitted
    under. A new protocol version is a new hash; the candidate's claim was under the old."""
    _admitted(w)
    w.predict("2", T0, 400, SHARP)
    w.ledger.protocols[OTHER_HASH] = CertifiedProtocol("agent", VALUES)
    w.now = T0 + 90 * DAY
    [rejected] = w.autonomy(OTHER_HASH).step(mode=Mode.DEMO)
    assert rejected.action is Action.REJECT and "PROTOCOL_CHANGED" in rejected.reasons
    assert w.life.champion(MODEL) is None


def test_admission_is_checked_again_at_promotion(w: World) -> None:
    _admitted(w)
    w.predict("2", T0, 400, SHARP)
    w.ledger.claims[HASH] = Claim(CONFIG, True, True, False, ())  # an erratum on the holdout
    w.now = T0 + 90 * DAY
    [rejected] = w.loop.step(mode=Mode.DEMO)
    assert rejected.action is Action.REJECT and "HOLDOUT_NOT_PASSED" in rejected.reasons


# --- the kill switches --------------------------------------------------------------------------


def _system_champions(w: World) -> None:
    """v1 then v2, both promoted by the loop; v2 is champion from T0 + 90 days."""
    _admitted(w, "1")
    w.predict("1", T0, 400, SHARP)
    w.now = T0 + 90 * DAY
    assert w.loop.step(mode=Mode.DEMO)[0].action is Action.PROMOTE
    w.now = T0 + 91 * DAY
    _admitted(w, "2")
    w.predict("1", w.now, 400, SHARP)
    w.predict("2", w.now, 400, SHARPER)
    w.now = T0 + 181 * DAY
    assert w.loop.step(mode=Mode.DEMO)[0].action is Action.PROMOTE


def test_brier_drift_rolls_back_to_the_previous_champion(w: World) -> None:
    _system_champions(w)
    w.now += DAY
    w.predict("2", w.now - DAY, 20, CLIMATE)  # 20 bars at about 0.62 against its 0.004
    w.now += DAY
    [killed] = w.loop.step(mode=Mode.DEMO)
    assert killed.action is Action.ROLLBACK and killed.reasons == ["BRIER_CUSUM"]
    assert (killed.champion_before, killed.champion_after) == ("2", "1")
    assert float(killed.metrics["brier_cusum"]) > 5
    assert w.life.champion(MODEL) == "1"
    v2 = w.registry.eligibility("model", f"{MODEL}@2")
    assert v2 is not None and v2.eligibility is Eligibility.DISABLED
    actions = [r[0] for r in w.conn.execute(
        "SELECT action FROM audit_log WHERE actor = ? ORDER BY id", (SYSTEM_ACTOR,))]  # fmt: skip
    assert "learning.rollback" in actions and "learning.autonomy.rollback" in actions


def test_a_killed_model_never_comes_back_on_its_own(w: World) -> None:
    _system_champions(w)
    w.predict("2", w.now, 20, CLIMATE)
    w.now += 2 * DAY
    assert w.loop.step(mode=Mode.DEMO)[0].action is Action.ROLLBACK
    w.predict("2", w.now, 500, SHARPER)
    w.now += 100 * DAY
    for _ in range(3):
        assert all(r.version != "2" or r.action is not Action.PROMOTE
                   for r in w.loop.step(mode=Mode.DEMO))  # fmt: skip
    assert w.life.champion(MODEL) != "2"
    assert w.loop.submit("2", protocol_hash=HASH, mode=Mode.DEMO) == ["ALREADY_SUBMITTED"]


def test_a_breach_with_no_previous_champion_withdraws_to_no_model(w: World) -> None:
    _admitted(w)
    w.predict("2", T0, 400, SHARP)
    w.now = T0 + 90 * DAY
    w.loop.step(mode=Mode.DEMO)
    w.predict("2", w.now, 30, CLIMATE)
    w.now += 2 * DAY
    [killed] = w.loop.step(mode=Mode.DEMO)
    assert killed.action is Action.WITHDRAW and killed.champion_after is None
    assert "rollback_refused" in killed.metrics
    assert w.life.champion(MODEL) is None
    assert w.loop.step(mode=Mode.DEMO) == []  # idempotent: nothing left to kill


def test_realised_demo_expectancy_below_the_floor_kills_in_demo_only(w: World) -> None:
    _admitted(w)
    w.predict("2", T0, 400, SHARP)
    w.now = T0 + 90 * DAY
    w.loop.step(mode=Mode.DEMO)
    w.nets = [D("-2.50")] * 30
    w.now += DAY
    assert w.loop.step(mode=Mode.SHADOW) == []  # no fills in SHADOW: not judged
    [killed] = w.loop.step(mode=Mode.DEMO)
    assert killed.reasons == ["DEMO_EXPECTANCY"] and killed.metrics["demo_expectancy_usd"]


def test_feature_drift_kills_on_recorded_snapshots(w: World) -> None:
    """PSI on the card's features, from the recorded snapshots: the window before its
    tenure against the latest one."""
    key = "M15.direction_10.direction"
    w.champion("1", feature_schema=FEATURE_SCHEMA, required_features=[key])
    log = BarLog(w.conn)
    for i in range(480):  # 5 days before T0, then 5 after, every 30 minutes
        as_of = T0 - 5 * DAY + i * timedelta(minutes=30)
        value = D(i % 10) if as_of < T0 else D(i % 10) + 20
        log.record(FeatureSnapshot(
            symbol="XAUUSD", feature_schema=FEATURE_SCHEMA, values={key: value},
            quality=DataQuality.OK, as_of=as_of, available_at=as_of,
            valid_until=as_of + timedelta(minutes=15)), timeframe=Timeframe.M15,
            provenance=Provenance.LIVE, mode=Mode.DEMO, dataset_version="v1")  # fmt: skip
    w.now = T0 + 5 * DAY
    [killed] = w.loop.step(mode=Mode.DEMO)
    assert killed.reasons == [f"PSI:{key}"] and killed.action is Action.WITHDRAW


def test_a_champion_whose_files_are_gone_is_withdrawn(w: World) -> None:
    w.champion("1")
    del w.artifacts.files_[f"{MODEL}@1"]
    w.now += DAY
    [killed] = w.loop.step(mode=Mode.DEMO)
    assert killed.reasons == ["NO_ARTIFACT"] and w.life.champion(MODEL) is None


def test_the_owner_live_champion_is_never_touched(w: World) -> None:
    """Breaching or not, an approval that names LIVE is the owner's: the loop leaves it."""
    w.candidate("1", supported_modes=[Mode.SHADOW, Mode.DEMO, Mode.LIVE])
    w.life.promote(MODEL, "1", modes=[Mode.DEMO, Mode.LIVE], evidence_refs=["x02:run:old"],
                   policy_ref="p", approved_by=OWNER_ACTOR, approval_ref="owner",
                   live_qualification_ref=LIVE_REF)  # fmt: skip
    w.predict("1", T0, 30, CLIMATE)
    w.now = T0 + 3 * DAY
    assert w.loop.step(mode=Mode.DEMO) == []
    assert w.life.champion(MODEL) == "1"
    rec = w.registry.eligibility("model", f"{MODEL}@1")
    assert rec is not None and rec.approves(Mode.LIVE, w.now)


def test_a_lapsed_champion_is_left_lapsed(w: World) -> None:
    w.champion("1")
    rec = w.registry.eligibility("model", f"{MODEL}@1")
    assert rec is not None
    w.registry.set_eligibility(rec.model_copy(update={"expires_at": T0 + DAY}),
                               actor=OWNER_ACTOR)  # fmt: skip
    w.predict("1", T0, 30, CLIMATE)
    w.now = T0 + 3 * DAY
    assert w.loop.step(mode=Mode.DEMO) == []  # M05 already waits; nothing to kill or renew
    assert w.registry.eligibility("model", f"{MODEL}@1") is None


# --- renewal ----------------------------------------------------------------------------------


def test_a_healthy_champion_is_renewed_only_with_recent_forward_evidence(w: World) -> None:
    _admitted(w)
    w.predict("2", T0, 400, SHARP)
    w.now = T0 + 90 * DAY
    w.loop.step(mode=Mode.DEMO)  # promoted, approved for the TTL
    expires = w.now + TTL
    w.now = expires - timedelta(hours=30)
    assert w.loop.step(mode=Mode.DEMO) == []  # not yet within renew_within_hours
    w.now = expires - timedelta(hours=20)
    assert w.loop.step(mode=Mode.DEMO) == []  # resolved rows are older than the TTL
    rec = w.registry.eligibility("model", f"{MODEL}@2")
    assert rec is not None and rec.expires_at == expires
    w.predict("2", w.now - timedelta(hours=10), 5, SHARP)
    [renewed] = w.loop.step(mode=Mode.DEMO)
    assert renewed.action is Action.RENEW and renewed.approval_before is not None
    rec = w.registry.eligibility("model", f"{MODEL}@2")
    assert rec is not None and rec.expires_at == w.now + TTL
    w.now += TTL + HOUR
    assert w.loop.step(mode=Mode.DEMO) == []  # lapsed: never resurrected
    assert w.registry.eligibility("model", f"{MODEL}@2") is None


# --- the pure kill rule -------------------------------------------------------------------------


def _policy() -> PromotionPolicy:
    return PromotionPolicy.from_protocol(HASH, Ledger().certified(HASH))


def _scored(n: int, q: float | None) -> Scored:
    return {T0 + i * HOUR: _row(T0 + i * HOUR, q) for i in range(n)}


def test_cusum_stays_quiet_at_the_reference_and_fires_on_a_sustained_rise() -> None:
    quiet, m = breaches(_policy(), _scored(500, SHARP), reference_brier=0.015, features=None,
                        demo_nets=[])  # fmt: skip
    assert quiet == [] and float(m["brier_cusum"]) == 0
    loud, _ = breaches(_policy(), _scored(15, CLIMATE), reference_brier=0.015, features=None,
                       demo_nets=[])  # fmt: skip
    assert loud == ["BRIER_CUSUM"]
    none, m = breaches(_policy(), _scored(15, CLIMATE), reference_brier=None, features=None,
                       demo_nets=[])  # fmt: skip
    assert none == [] and "brier_cusum" not in m


def test_ece_is_judged_only_on_enough_resolved_labels() -> None:
    def off(n: int) -> Scored:  # always 0.9 on UP, but UP only half the time
        return {T0 + i * HOUR: ((0.9, 0.05, 0.05), PATTERN[i % 10]) for i in range(n)}

    assert breaches(_policy(), off(299), reference_brier=None, features=None,
                    demo_nets=[])[0] == []  # fmt: skip
    off = off(300)  # type: ignore[assignment]
    assert breaches(_policy(), off, reference_brier=None, features=None,
                    demo_nets=[])[0] == ["ECE"]  # fmt: skip


def test_psi_breach_and_a_missing_feature_both_kill() -> None:
    ref = [D(i % 10) for i in range(100)]
    same, _ = breaches(_policy(), {}, reference_brier=None, features=({"a": ref}, {"a": ref}),
                       demo_nets=[])  # fmt: skip
    assert same == []
    moved = [x + 20 for x in ref]
    drift, _ = breaches(_policy(), {}, reference_brier=None,
                        features=({"a": ref, "b": ref}, {"a": moved, "b": []}),
                        demo_nets=[])  # fmt: skip
    assert drift == ["PSI:a", "PSI_MISSING:b"]


def test_demo_expectancy_needs_enough_closed_cycles() -> None:
    few = [D(-5)] * 29
    assert breaches(_policy(), {}, reference_brier=None, features=None, demo_nets=few)[0] == []
    assert breaches(_policy(), {}, reference_brier=None, features=None,
                    demo_nets=[*few, D(-5)])[0] == ["DEMO_EXPECTANCY"]  # fmt: skip


# --- the real resolver's filter ---------------------------------------------------------------


def test_the_resolver_keeps_live_class_labelled_rows_of_the_current_label(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.contracts import Confidence, PredictionGenerated

    def pred(version_label: str, with_classes: bool = True) -> PredictionGenerated:
        return PredictionGenerated(
            prediction_id="pred-1", model_id=MODEL, model_version="1",
            lineage=ForecastLineage.CHART_ONLY, horizon=Timeframe.H1, horizon_bars=1,
            direction=Direction.UP,
            probability=Confidence(value=D("0.6"), semantics="s", calibrated=True),
            data_quality=DataQuality.OK, input_refs=["snap"], generated_at=T0,
            valid_until=T0 + DAY,
            class_probabilities=ClassProbabilities(label_version=version_label, up=D("0.6"),
                                                   down=D("0.3"), flat=D("0.1"))
            if with_classes else None,
        )  # fmt: skip

    def lab(outcome: Outcome) -> TripleBarrierLabel:
        return TripleBarrierLabel(LABEL_VERSION, outcome, D(1), D(1), Timeframe.M5, T0, T0)

    t = [T0 + i * timedelta(hours=1) for i in range(6)]
    rows = [
        (RecordedPrediction(t[0], Provenance.LIVE, pred(LABEL_VERSION)), lab(Outcome.DOWN)),
        (RecordedPrediction(t[1], Provenance.BACKFILL, pred(LABEL_VERSION)), lab(Outcome.UP)),
        (RecordedPrediction(t[2], Provenance.LIVE, pred(LABEL_VERSION)), lab(Outcome.AMBIGUOUS)),
        (RecordedPrediction(t[3], Provenance.LIVE, pred(LABEL_VERSION)), None),
        (RecordedPrediction(t[4], Provenance.LIVE, pred("m07.labels.tb.v1")), lab(Outcome.UP)),
        (RecordedPrediction(t[5], Provenance.LIVE, pred(LABEL_VERSION, False)), lab(Outcome.UP)),
    ]  # fmt: skip
    monkeypatch.setattr(autonomy_module, "prediction_labels", lambda *a, **k: rows)
    resolve = label_resolver(conn, CandleStore(conn, dataset_version="v1"), symbol="XAUUSD",
                             timeframe=Timeframe.M15, session=None)  # fmt: skip
    assert resolve(MODEL, "1", T0, T0 + DAY) == {t[0]: ((0.6, 0.3, 0.1), 1)}


# --- hard guards on what the loop may touch -----------------------------------------------------

ALLOWED_WRITES = {"registry_eligibility", "learning_promotions", "learning_autonomy",
                  "audit_log"}  # fmt: skip
"""Everything a promotion may write. Risk config, M06, the journal, readiness policies
and live qualifications are not on it."""


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def test_a_promotion_never_touches_the_risk_config_or_m06(w: World) -> None:
    """Guard: the loop writes only eligibility, the champion log, its own records and the
    audit. It never mints a Live qualification or saves a readiness policy."""
    w.champion("1")
    _admitted(w)
    w.predict("1", T0, 400, CLIMATE)
    w.predict("2", T0, 400, SHARP)
    w.now = T0 + 90 * DAY
    before = _counts(w.conn)
    assert w.loop.step(mode=Mode.DEMO)[0].action is Action.PROMOTE
    after = _counts(w.conn)
    changed = {t for t in after if after[t] != before[t]}
    assert changed <= ALLOWED_WRITES, changed - ALLOWED_WRITES
    assert after["live_qualifications"] == 0 and after["readiness_policies"] == 0


def _imports(module: object) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


FORBIDDEN = ("app.news", "app.risk", "app.engine", "app.execution", "app.profit",
             "app.validation.evidence")  # fmt: skip
"""Not imported by the loop: LLM/news output, M06, the broker side and the readiness gate
that mints Live refs."""
NEVER_LOADED = ("app.news", "app.risk", "app.engine", "app.profit", "app.execution.executor",
                "app.execution.ledger", "app.execution.fake_broker",
                "app.validation.system_backtest")  # fmt: skip
"""Not even loaded through another module. (`app.execution.broker` is loaded: M02 and M05
take their quote and symbol types from it. The lifecycle reads one constant from the
readiness gate.)"""


@pytest.mark.parametrize("module", [autonomy_module, runtime_module])
def test_llm_output_and_m06_are_out_of_the_promotion_path(module: object) -> None:
    """Guard: the loop imports nothing from the news/LLM modules, M06, the executor or the
    readiness gate, and none of those is even loaded with it, so no LLM output can drive
    a promotion and the loop cannot reach the risk config or an order."""
    assert not {m for m in _imports(module) if m.startswith(FORBIDDEN)}
    name = module.__name__  # type: ignore[attr-defined]
    code = f"import sys, {name}; print('\\n'.join(sys.modules))"
    loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            check=True).stdout.split()  # fmt: skip
    assert [m for m in loaded if m.startswith(NEVER_LOADED)] == []
