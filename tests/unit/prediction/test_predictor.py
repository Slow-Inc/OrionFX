"""M04 slice A: predictor port, model cards, approval-gated loading, baseline (#46; PRE-02,
PRE-04; AT-16 unapproved artifact refused; AT-17 separate lineage, semantics not
mislabelled). No model is trained here: predictors are fakes with cards."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.contracts import EligibilityRecord, FeatureSnapshot, ParameterBound
from app.core.enums import DataQuality, Direction, Eligibility, ForecastLineage, Mode, Timeframe
from app.prediction.baseline import BASELINE
from app.prediction.card import ModelCard
from app.prediction.engine import Refused, load_predictor, predict
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
HASH = "sha256:" + "a" * 64


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "pred.db")
    migrate(c)
    yield c
    c.close()


def _card(**over: object) -> ModelCard:
    base: dict[str, object] = {
        "model_id": "toy",
        "model_version": "1.0.0",
        "artifact_hash": HASH,
        "lineage": ForecastLineage.CHART_ONLY,
        "feature_schema": "m02.features.v1",
        "required_features": ["H1.rsi_14.rsi", "H1.atr_14.atr"],
        "feature_ranges": {"H1.rsi_14.rsi": ParameterBound(low=D(5), high=D(95)),
                           "H1.atr_14.atr": ParameterBound(low=D("0.5"), high=D(30))},
        "horizon": Timeframe.H1,
        "horizon_bars": 3,
        "semantics": "uncalibrated model score for close-up over 3 H1 bars",
        "calibrated": False,
        "validation": {"brier": D("0.24"), "baseline_brier": D("0.25")},
        "evidence_refs": ["x02:wf-1"],
    }  # fmt: skip
    base.update(over)
    return ModelCard.model_validate(base)


class Toy:
    """A fake predictor: no training, just a fixed rule, with a card."""

    def __init__(self, card: ModelCard) -> None:
        self.card = card

    def predict(self, features: dict[str, Decimal]) -> dict[Direction, Decimal]:
        up = D("0.6") if features["H1.rsi_14.rsi"] > 50 else D("0.4")
        return {Direction.UP: up, Direction.DOWN: 1 - up}


def _snapshot(**values: Decimal | None) -> FeatureSnapshot:
    base: dict[str, Decimal | None] = {
        "H1.rsi_14.rsi": D(60),
        "H1.atr_14.atr": D(4),
        "H1.direction_10.direction": D(1),
    }
    base.update(values)
    return FeatureSnapshot(
        symbol="XAUUSD",
        feature_schema="m02.features.v1",
        values=base,
        quality=DataQuality.OK,
        as_of=T0,
        available_at=T0 + timedelta(seconds=1),
        valid_until=T0 + timedelta(minutes=5),
    )


def _register(
    conn: sqlite3.Connection, card: ModelCard, *, modes: list[Mode], **elig: object
) -> ArtifactRegistry:
    reg = ArtifactRegistry(conn)
    reg.register("model", card.manifest(), actor="m07")
    record: dict[str, object] = {
        "subject_hash": card.artifact_hash,
        "subject_kind": "model",
        "subject_ref": f"{card.model_id}@{card.model_version}",
        "user_enabled": True,
        "eligibility": Eligibility.DEMO_ELIGIBLE,
        "approved_modes": modes,
        "context": {"symbol": "XAUUSD"},
        "evidence_refs": ["x02:wf-1"],
        "policy_version": "p1",
        "expires_at": None,
        "revoked_at": None,
    }
    record.update(elig)
    reg.set_eligibility(EligibilityRecord.model_validate(record), actor="owner")
    return reg


# --- loading (AT-16) ----------------------------------------------------------------------------


def test_an_approved_model_loads_for_its_mode(conn: sqlite3.Connection) -> None:
    card = _card()
    reg = _register(conn, card, modes=[Mode.SHADOW, Mode.DEMO])
    assert load_predictor(Toy(card), reg, mode=Mode.DEMO, now=NOW).card == card


def test_unregistered_or_unapproved_models_are_refused(conn: sqlite3.Connection) -> None:
    card = _card()
    reg = ArtifactRegistry(conn)
    with pytest.raises(Refused, match="not registered"):
        load_predictor(Toy(card), reg, mode=Mode.DEMO, now=NOW)
    reg.register("model", card.manifest(), actor="m07")
    with pytest.raises(Refused, match="not approved"):
        load_predictor(Toy(card), reg, mode=Mode.DEMO, now=NOW)


def test_a_different_artifact_hash_is_refused(conn: sqlite3.Connection) -> None:
    reg = _register(conn, _card(), modes=[Mode.DEMO])
    tampered = _card(artifact_hash="sha256:" + "b" * 64)
    with pytest.raises(Refused, match="hash"):
        load_predictor(Toy(tampered), reg, mode=Mode.DEMO, now=NOW)


def test_mode_expiry_and_revocation_are_enforced(conn: sqlite3.Connection) -> None:
    card = _card()
    reg = _register(conn, card, modes=[Mode.SHADOW], eligibility=Eligibility.SHADOW_ONLY)
    with pytest.raises(Refused):
        load_predictor(Toy(card), reg, mode=Mode.DEMO, now=NOW)
    reg.set_eligibility(
        EligibilityRecord.model_validate(
            {"subject_hash": HASH, "subject_kind": "model", "subject_ref": "toy@1.0.0",
             "user_enabled": True, "eligibility": Eligibility.DEMO_ELIGIBLE,
             "approved_modes": [Mode.DEMO], "context": {}, "evidence_refs": ["e"],
             "policy_version": "p", "expires_at": T0, "revoked_at": None}
        ),
        actor="owner",
    )  # fmt: skip
    with pytest.raises(Refused):  # expired at T0, asked at NOW
        load_predictor(Toy(card), reg, mode=Mode.DEMO, now=NOW)


def test_the_baseline_needs_no_artifact_but_never_runs_live(conn: sqlite3.Connection) -> None:
    reg = ArtifactRegistry(conn)
    assert load_predictor(BASELINE, reg, mode=Mode.SHADOW, now=NOW) is BASELINE
    with pytest.raises(Refused, match="LIVE"):
        load_predictor(BASELINE, reg, mode=Mode.LIVE, now=NOW)


# --- prediction ---------------------------------------------------------------------------------


def test_prediction_carries_lineage_semantics_and_validity() -> None:
    p = predict(Toy(_card()), _snapshot(), now=NOW)
    assert p.direction is Direction.UP and p.probability.value == D("0.6")
    assert p.lineage is ForecastLineage.CHART_ONLY
    assert p.probability.calibrated is False and "uncalibrated" in p.probability.semantics
    assert p.valid_until == T0 + timedelta(minutes=5)  # snapshot expires before 3 H1 bars
    assert p.input_refs == [f"m02.features.v1@{T0.isoformat()}"]
    assert p.model_id == "toy" and p.horizon is Timeframe.H1 and p.horizon_bars == 3


def test_news_enhanced_models_record_their_news_inputs() -> None:
    card = _card(lineage=ForecastLineage.NEWS_ENHANCED)
    p = predict(Toy(card), _snapshot(), now=NOW, news_assessment_ids=["news-1"])
    assert p.lineage is ForecastLineage.NEWS_ENHANCED and "news-1" in p.input_refs
    with pytest.raises(Refused, match="news"):
        predict(Toy(card), _snapshot(), now=NOW)  # news-enhanced without news


def test_missing_feature_is_refused() -> None:
    with pytest.raises(Refused, match="MISSING_FEATURE"):
        predict(Toy(_card()), _snapshot(**{"H1.atr_14.atr": None}), now=NOW)


def test_out_of_distribution_input_is_refused() -> None:
    with pytest.raises(Refused, match="OUT_OF_DISTRIBUTION"):
        predict(Toy(_card()), _snapshot(**{"H1.atr_14.atr": D(80)}), now=NOW)


@pytest.mark.parametrize("quality", [DataQuality.STALE, DataQuality.MISSING])
def test_unusable_snapshot_is_refused(quality: DataQuality) -> None:
    snap = _snapshot().model_copy(update={"quality": quality})
    with pytest.raises(Refused):
        predict(Toy(_card()), snap, now=NOW)


def test_expired_or_future_snapshot_is_refused() -> None:
    with pytest.raises(Refused):
        predict(Toy(_card()), _snapshot(), now=T0 + timedelta(hours=1))
    with pytest.raises(Refused):
        predict(Toy(_card()), _snapshot(), now=T0)  # not yet available


def test_schema_mismatch_is_refused() -> None:
    snap = _snapshot().model_copy(update={"feature_schema": "m02.features.v9"})
    with pytest.raises(Refused, match="schema"):
        predict(Toy(_card()), snap, now=NOW)


def test_baseline_follows_the_h1_trend_and_says_it_is_a_baseline() -> None:
    p = predict(BASELINE, _snapshot(), now=NOW)
    assert p.direction is Direction.UP and p.probability.value == D("0.5")
    assert "baseline" in p.probability.semantics and not p.probability.calibrated
    flat = predict(BASELINE, _snapshot(**{"H1.direction_10.direction": D(0)}), now=NOW)
    assert flat.direction is Direction.NEUTRAL


def test_card_needs_evidence_to_claim_calibration() -> None:
    with pytest.raises(ValueError):
        _card(calibrated=True, evidence_refs=[])


def test_loader_judges_expiry_at_its_own_now(conn: sqlite3.Connection) -> None:
    card = _card()
    reg = ArtifactRegistry(conn, clock=lambda: T0)  # the registry still thinks it is valid
    reg.register("model", card.manifest(), actor="m07")
    reg.set_eligibility(
        EligibilityRecord.model_validate(
            {"subject_hash": HASH, "subject_kind": "model", "subject_ref": "toy@1.0.0",
             "user_enabled": True, "eligibility": Eligibility.DEMO_ELIGIBLE,
             "approved_modes": [Mode.DEMO], "context": {}, "evidence_refs": ["e"],
             "policy_version": "p", "expires_at": T0 + timedelta(seconds=2), "revoked_at": None}
        ),
        actor="owner",
    )  # fmt: skip
    assert load_predictor(Toy(card), reg, mode=Mode.DEMO, now=T0 + timedelta(seconds=1))
    with pytest.raises(Refused):
        load_predictor(Toy(card), reg, mode=Mode.DEMO, now=NOW)
