"""M07 slice C: dataset manifests, model promotion with evidence, rollback and drift (#61;
LRN-04/05/06; AT-13, AT-16, AT-35). Nothing trains here and nothing promotes itself:
a model becomes champion only with evidence, a policy and the owner's approval."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.enums import ForecastLineage, Mode, Timeframe
from app.learning.lifecycle import (
    DatasetManifest,
    DatasetStore,
    LifecycleRefused,
    ModelLifecycle,
    Split,
    drift_report,
    psi,
)
from app.prediction.card import ModelCard
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "life.db")
    migrate(c)
    yield c
    c.close()


def _dataset(version: str = "1", **over: object) -> DatasetManifest:
    base: dict[str, object] = {
        "dataset_id": "xau-h1", "version": version, "source_refs": ["candles:v1"],
        "window_start": T0, "window_end": T0 + 90 * DAY,
        "splits": [Split(name="train", start=T0, end=T0 + 60 * DAY),
                   Split(name="validation", start=T0 + 60 * DAY, end=T0 + 75 * DAY),
                   Split(name="holdout", start=T0 + 75 * DAY, end=T0 + 90 * DAY)],
        "content_hash": "sha256:" + "a" * 64,
    }  # fmt: skip
    base.update(over)
    return DatasetManifest.model_validate(base)


def _model(version: str, sha: str, modes: list[Mode] | None = None) -> ModelCard:
    return ModelCard(
        model_id="xgb-dir", model_version=version, artifact_hash="sha256:" + sha * 64,
        lineage=ForecastLineage.CHART_ONLY, feature_schema="m02.features.v2",
        required_features=["k"], feature_ranges={}, horizon=Timeframe.H1, horizon_bars=1,
        semantics="calibrated P(dir)", calibrated=False, validation={"brier": D("0.24")},
        evidence_refs=[], supported_modes=modes or [Mode.SHADOW, Mode.DEMO, Mode.LIVE],
    )  # fmt: skip


def _x02(ref: str, subject: str) -> bool:
    """Stands in for X02's verifier: one real qualification, for xgb-dir@1 only."""
    return ref == "x02:live-qualification:r7" and subject == "model:xgb-dir@1"


@pytest.fixture
def life(conn: sqlite3.Connection) -> ModelLifecycle:
    reg = ArtifactRegistry(conn, clock=lambda: T0)
    lc = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: T0 + DAY,
                        live_verifier=_x02)  # fmt: skip
    lc.datasets.save(_dataset())
    return lc


# --- datasets ---------------------------------------------------------------------------------


def test_splits_must_be_ordered_non_overlapping_and_inside_the_window() -> None:
    _dataset()  # valid
    overlap = [Split(name="train", start=T0, end=T0 + 60 * DAY),
               Split(name="validation", start=T0 + 50 * DAY, end=T0 + 70 * DAY)]  # fmt: skip
    with pytest.raises(ValidationError, match="overlap"):
        _dataset(splits=overlap)
    outside = [Split(name="train", start=T0 - DAY, end=T0 + 10 * DAY),
               Split(name="validation", start=T0 + 10 * DAY, end=T0 + 20 * DAY)]  # fmt: skip
    with pytest.raises(ValidationError, match="window"):
        _dataset(splits=outside)
    with pytest.raises(ValidationError, match="order"):
        _dataset(splits=[Split(name="train", start=T0, end=T0 + 10 * DAY)])
    validation_first = [Split(name="validation", start=T0, end=T0 + 15 * DAY),
                        Split(name="train", start=T0 + 15 * DAY, end=T0 + 75 * DAY)]  # fmt: skip
    with pytest.raises(ValidationError, match="order"):
        _dataset(splits=validation_first)


def test_dataset_versions_are_immutable(life: ModelLifecycle) -> None:
    life.datasets.save(_dataset())  # identical: no-op
    with pytest.raises(LifecycleRefused):
        life.datasets.save(_dataset(content_hash="sha256:" + "b" * 64))
    assert life.datasets.get("xau-h1", "1") == _dataset()


# --- candidates and promotion -------------------------------------------------------------------


def test_a_candidate_needs_a_known_dataset(life: ModelLifecycle) -> None:
    with pytest.raises(LifecycleRefused, match="dataset"):
        life.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@9"])
    with pytest.raises(LifecycleRefused, match="dataset"):
        life.propose_candidate(_model("1", "1"), dataset_refs=[])
    with pytest.raises(LifecycleRefused, match="malformed"):
        life.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1"])
    cand = life.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
    assert cand.model_id == "xgb-dir" and cand.artifact_sha256 == "1" * 64
    assert cand.evaluation == {"brier": D("0.24")}  # from the card, one source of truth
    assert life.candidate("xgb-dir", "1") == cand


def test_promotion_needs_evidence_policy_and_the_owner(life: ModelLifecycle) -> None:
    life.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
    with pytest.raises(LifecycleRefused, match="evidence"):
        life.promote("xgb-dir", "1", modes=[Mode.DEMO], evidence_refs=[], policy_ref="p",
                     approved_by="owner", approval_ref="a1")  # fmt: skip
    for not_owner in ("m07", "m09", "scheduler", "owner-bot", "OWNER "):
        with pytest.raises(LifecycleRefused, match="owner"):
            life.promote("xgb-dir", "1", modes=[Mode.DEMO], evidence_refs=["x02:r1"],
                         policy_ref="p", approved_by=not_owner, approval_ref="a1")  # fmt: skip
    approved = life.promote("xgb-dir", "1", modes=[Mode.SHADOW, Mode.DEMO],
                            evidence_refs=["x02:r1"], policy_ref="p", approved_by="owner",
                            approval_ref="a1")  # fmt: skip
    assert approved.allowed_modes == [Mode.SHADOW, Mode.DEMO]
    assert life.champion("xgb-dir") == "1"
    rec = life.registry.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.approves(Mode.DEMO, T0 + DAY)
    assert not rec.approves(Mode.LIVE, T0 + DAY)


def test_an_unproposed_model_cannot_be_promoted(life: ModelLifecycle) -> None:
    with pytest.raises(LifecycleRefused, match="candidate"):
        life.promote("xgb-dir", "7", modes=[Mode.DEMO], evidence_refs=["e"], policy_ref="p",
                     approved_by="owner", approval_ref="a")  # fmt: skip


def test_live_needs_live_evidence_and_is_never_implied(life: ModelLifecycle) -> None:
    life.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
    for bad in (None, "x02:no-live-qualification", "x02:live-qualification-failed:r3"):
        with pytest.raises(LifecycleRefused, match="live"):
            life.promote("xgb-dir", "1", modes=[Mode.LIVE], evidence_refs=["x02:r1"],
                         policy_ref="p", approved_by="owner", approval_ref="a1",
                         live_qualification_ref=bad)  # fmt: skip
    life.promote("xgb-dir", "1", modes=[Mode.SHADOW, Mode.DEMO, Mode.LIVE],
                 evidence_refs=["x02:r1"], policy_ref="p", approved_by="owner",
                 approval_ref="a1", live_qualification_ref="x02:live-qualification:r7")  # fmt: skip
    rec = life.registry.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.approves(Mode.LIVE, T0 + DAY)


# --- champion and rollback -----------------------------------------------------------------------


def _two_champions(life: ModelLifecycle) -> None:
    for version, sha in (("1", "1"), ("2", "2")):
        life.propose_candidate(_model(version, sha), dataset_refs=["xau-h1@1"])
        life.promote("xgb-dir", version, modes=[Mode.DEMO], evidence_refs=[f"x02:{version}"],
                     policy_ref="p", approved_by="owner", approval_ref=f"a{version}")  # fmt: skip


def test_a_new_champion_disables_the_previous_one(life: ModelLifecycle) -> None:
    _two_champions(life)
    assert life.champion("xgb-dir") == "2"
    old = life.registry.eligibility("model", "xgb-dir@1")
    assert old is not None and not old.approves(Mode.DEMO, T0 + DAY)


def test_rollback_restores_the_previous_champion(life: ModelLifecycle) -> None:
    _two_champions(life)
    restored = life.rollback("xgb-dir", approved_by="owner", reason="drawdown on v2")
    assert restored == "1" and life.champion("xgb-dir") == "1"
    v1 = life.registry.eligibility("model", "xgb-dir@1")
    v2 = life.registry.eligibility("model", "xgb-dir@2")
    assert v1 is not None and v1.approves(Mode.DEMO, T0 + DAY) and v1.evidence_refs == ["x02:1"]
    assert v2 is not None and not v2.approves(Mode.DEMO, T0 + DAY)


def test_rollback_needs_a_previous_champion_and_the_owner(life: ModelLifecycle) -> None:
    life.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
    life.promote("xgb-dir", "1", modes=[Mode.DEMO], evidence_refs=["e"], policy_ref="p",
                 approved_by="owner", approval_ref="a")  # fmt: skip
    with pytest.raises(LifecycleRefused, match="previous"):
        life.rollback("xgb-dir", approved_by="owner", reason="r")
    _two_champions(life)
    with pytest.raises(LifecycleRefused, match="owner"):
        life.rollback("xgb-dir", approved_by="system", reason="r")


def test_every_lifecycle_change_is_audited(conn: sqlite3.Connection, life: ModelLifecycle) -> None:
    _two_champions(life)
    life.rollback("xgb-dir", approved_by="owner", reason="r")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions.count("learning.promote") == 2 and actions.count("learning.rollback") == 1


# --- drift ---------------------------------------------------------------------------------------


def test_psi_matches_a_hand_computed_value() -> None:
    reference = [D(x) for x in (1, 1, 2, 2, 3, 3, 4, 4)]
    recent = [D(x) for x in (1, 2, 3, 4, 4, 4, 4, 4)]
    # bins from reference quartiles: [1], [2], [3], [4] -> ref 0.25 each;
    # recent 1/8, 1/8, 1/8, 5/8 -> PSI = sum((r - e) ln(r / e))
    value = psi(reference, recent, bins=4)
    assert value == D("0.6035")  # 3 x (0.125 - 0.25) ln 0.5 + (0.625 - 0.25) ln 2.5


def test_identical_windows_have_zero_drift_and_no_recommendation() -> None:
    xs = [D(x) for x in range(20)]
    report = drift_report({"rsi": xs}, {"rsi": xs})
    assert report.psi["rsi"] == 0 and not report.retrain_recommended


def test_drift_recommends_a_retrain_but_never_trains(
    conn: sqlite3.Connection, life: ModelLifecycle
) -> None:
    ref = [D(x) for x in range(20)]
    shifted = [D(x + 15) for x in range(20)]
    report = drift_report({"rsi": ref, "adx": ref}, {"rsi": shifted, "adx": ref})
    assert report.retrain_recommended and report.drifted == ["rsi"]
    life.record_drift("xgb-dir", report)
    (detail,) = conn.execute(
        "SELECT detail FROM audit_log WHERE action = 'learning.retrain_recommended'"
    ).fetchone()
    assert "rsi" in detail
    assert life.champion("xgb-dir") is None  # nothing was trained or promoted


# --- review fixes ---


def test_a_model_registered_without_proposal_cannot_be_promoted(life: ModelLifecycle) -> None:
    life.registry.register("model", _model("5", "5").manifest(), actor="someone")
    with pytest.raises(LifecycleRefused, match="candidate"):
        life.promote("xgb-dir", "5", modes=[Mode.DEMO], evidence_refs=["e"], policy_ref="p",
                     approved_by="owner", approval_ref="a")  # fmt: skip


def test_unsupported_modes_are_refused_by_the_lifecycle(life: ModelLifecycle) -> None:
    life.propose_candidate(_model("1", "1", [Mode.SHADOW, Mode.DEMO]), dataset_refs=["xau-h1@1"])
    with pytest.raises(LifecycleRefused, match="support"):
        life.promote("xgb-dir", "1", modes=[Mode.LIVE], evidence_refs=["e"], policy_ref="p",
                     approved_by="owner", approval_ref="a",
                     live_qualification_ref="x02:live-qualification:r7")  # fmt: skip


def test_a_second_rollback_never_restores_the_version_just_rolled_away(
    life: ModelLifecycle,
) -> None:
    for version in ("1", "2", "3"):
        life.propose_candidate(_model(version, version), dataset_refs=["xau-h1@1"])
        life.promote("xgb-dir", version, modes=[Mode.DEMO], evidence_refs=[f"e{version}"],
                     policy_ref="p", approved_by="owner", approval_ref=f"a{version}")  # fmt: skip
    assert life.rollback("xgb-dir", approved_by="owner", reason="v3 bad") == "2"
    assert life.rollback("xgb-dir", approved_by="owner", reason="v2 bad too") == "1"
    with pytest.raises(LifecycleRefused, match="previous"):
        life.rollback("xgb-dir", approved_by="owner", reason="nothing left")


def test_rolling_back_to_an_expired_approval_is_refused(conn: sqlite3.Connection) -> None:
    reg = ArtifactRegistry(conn, clock=lambda: T0)
    now = [T0 + DAY]
    lc = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: now[0])
    lc.datasets.save(_dataset())
    lc.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
    lc.promote("xgb-dir", "1", modes=[Mode.DEMO], evidence_refs=["e"], policy_ref="p",
               approved_by="owner", approval_ref="a", expires_at=T0 + 10 * DAY)  # fmt: skip
    lc.propose_candidate(_model("2", "2"), dataset_refs=["xau-h1@1"])
    lc.promote("xgb-dir", "2", modes=[Mode.DEMO], evidence_refs=["e"], policy_ref="p",
               approved_by="owner", approval_ref="b")  # fmt: skip
    now[0] = T0 + 20 * DAY
    with pytest.raises(LifecycleRefused, match="expired"):
        lc.rollback("xgb-dir", approved_by="owner", reason="r")
    assert lc.champion("xgb-dir") == "2"


def test_a_missing_feature_counts_as_drift() -> None:
    ref = [D(x) for x in range(20)]
    report = drift_report({"rsi": ref, "adx": ref}, {"adx": ref})
    assert report.missing == ["rsi"] and report.retrain_recommended


def test_live_is_refused_without_the_x02_verifier_or_for_another_model(
    conn: sqlite3.Connection,
) -> None:
    reg = ArtifactRegistry(conn, clock=lambda: T0)
    bare = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: T0 + DAY)
    bare.datasets.save(_dataset())
    bare.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
    live = {"modes": [Mode.LIVE], "evidence_refs": ["e"], "policy_ref": "p",
            "approved_by": "owner", "approval_ref": "a",
            "live_qualification_ref": "x02:live-qualification:r7"}  # fmt: skip
    with pytest.raises(LifecycleRefused, match="verifier"):
        bare.promote("xgb-dir", "1", **live)  # type: ignore[arg-type]
    wired = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: T0 + DAY,
                           live_verifier=_x02)  # fmt: skip
    wired.propose_candidate(_model("2", "2"), dataset_refs=["xau-h1@1"])
    with pytest.raises(LifecycleRefused, match="not issued"):
        wired.promote("xgb-dir", "2", **live)  # type: ignore[arg-type]  # r7 is for v1 only
