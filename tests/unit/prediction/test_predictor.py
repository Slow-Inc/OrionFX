"""M04 slice A: predictor port, model cards, approval-gated loading, baseline (#46; PRE-02,
PRE-04; AT-16 unapproved artifact refused; AT-17 lineage and semantics). No model
is trained: the "model" is a tiny rule in a module file whose exact bytes are
hashed and registered, as a real artifact would be."""

import hashlib
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.contracts import EligibilityRecord, FeatureSnapshot, ParameterBound
from app.core.enums import DataQuality, Direction, Eligibility, ForecastLineage, Mode, Timeframe
from app.prediction.baseline import BASELINE
from app.prediction.card import ModelCard
from app.prediction.engine import LoadedPredictor, Refused, load_baseline, load_model, predict
from app.storage.artifacts import ArtifactLoader
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
SOURCE = """from decimal import Decimal as D

from app.core.enums import Direction
from app.prediction.card import ModelCard

CARD = ModelCard.model_validate_json({card!r})


class Toy:
    card = CARD

    def predict(self, features):
        {body}


def build():
    return Toy()
"""
RULE = (
    'up = D("0.6") if features["H1.rsi_14.rsi"] > 50 else D("0.4")\n'
    "        return {Direction.UP: up, Direction.DOWN: 1 - up}"
)


def _card(**over: object) -> ModelCard:
    base: dict[str, object] = {
        "model_id": "toy",
        "model_version": "1.0.0",
        "artifact_hash": None,  # stamped from the registry on load
        "lineage": ForecastLineage.CHART_ONLY,
        "feature_schema": "m02.features.v1",
        "required_features": ["H1.rsi_14.rsi", "H1.atr_14.atr"],
        "feature_ranges": {"H1.rsi_14.rsi": ParameterBound(low=D(5), high=D(95)),
                           "H1.atr_14.atr": ParameterBound(low=D("0.5"), high=D(30))},
        "horizon": Timeframe.H1,
        "horizon_bars": 3,
        "semantics": "uncalibrated model score over 3 H1 bars",
        "calibrated": False,
        "validation": {"brier": D("0.24"), "baseline_brier": D("0.25")},
        "evidence_refs": ["x02:wf-1"],
    }  # fmt: skip
    base.update(over)
    return ModelCard.model_validate(base)


def _elig(file_hash: str, version: str = "1.0.0", **over: object) -> EligibilityRecord:
    base: dict[str, object] = {
        "subject_hash": file_hash,
        "subject_kind": "model",
        "subject_ref": f"toy@{version}",
        "user_enabled": True,
        "eligibility": Eligibility.DEMO_ELIGIBLE,
        "approved_modes": [Mode.SHADOW, Mode.DEMO],
        "context": {},
        "evidence_refs": ["x02:wf-1"],
        "policy_version": "p1",
        "expires_at": None,
        "revoked_at": None,
    }
    base.update(over)
    return EligibilityRecord.model_validate(base)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "pred.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "toy_models"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield root
    for name in [m for m in sys.modules if m.startswith("toy_models")]:
        del sys.modules[name]


def _publish(
    conn: sqlite3.Connection, pkg: Path, *, version: str = "1.0.0", body: str = RULE, **elig: object
) -> tuple[ArtifactRegistry, ArtifactLoader, str]:
    """Write the model file, register its exact bytes, approve it."""
    card = _card(model_version=version)
    path = pkg / f"toy_{version.replace('.', '_')}.py"
    path.write_text(
        SOURCE.format(card=card.model_dump_json(), body=body), encoding="utf-8", newline="\n"
    )
    file_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    reg = ArtifactRegistry(conn)
    reg.register(
        "model", card.model_copy(update={"artifact_hash": file_hash}).manifest(), actor="m07"
    )
    reg.set_eligibility(_elig(file_hash, version, **elig), actor="owner")
    return reg, ArtifactLoader(reg, allowed_packages=("toy_models",)), f"toy_models.{path.stem}"


def _load(
    reg: ArtifactRegistry, loader: ArtifactLoader, module: str, *,
    mode: Mode = Mode.DEMO, version: str = "1.0.0", now: datetime = NOW,
) -> LoadedPredictor:  # fmt: skip
    return load_model(
        loader, reg, model_id="toy", version=version, module=module, mode=mode, now=now
    )


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


# --- loading (AT-16) ----------------------------------------------------------------------------


def test_an_approved_model_loads_with_its_verified_hash(
    conn: sqlite3.Connection, pkg: Path
) -> None:
    reg, loader, module = _publish(conn, pkg)
    loaded = _load(reg, loader, module)
    manifest = reg.get("model", "toy", "1.0.0")
    assert manifest is not None and loaded.card.artifact_hash == manifest.artifact_hash


def test_unapproved_mode_is_refused(conn: sqlite3.Connection, pkg: Path) -> None:
    reg, loader, module = _publish(
        conn, pkg, approved_modes=[Mode.SHADOW], eligibility=Eligibility.SHADOW_ONLY
    )
    with pytest.raises(Refused):
        _load(reg, loader, module, mode=Mode.DEMO)


def test_tampered_model_bytes_are_refused(conn: sqlite3.Connection, pkg: Path) -> None:
    reg, loader, module = _publish(conn, pkg)
    path = pkg / "toy_1_0_0.py"
    path.write_text(path.read_text(encoding="utf-8").replace('"0.6"', '"0.9"'), encoding="utf-8")
    with pytest.raises(Refused, match="hash"):
        _load(reg, loader, module)


def test_a_module_building_another_models_card_is_refused(
    conn: sqlite3.Connection, pkg: Path
) -> None:
    reg, loader, module = _publish(conn, pkg)  # the file builds the card of toy@1.0.0
    path = pkg / "toy_1_0_0.py"
    other = _card(model_version="3.0.0").model_copy(
        update={"artifact_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
    )
    reg.register("model", other.manifest(), actor="m07")
    reg.set_eligibility(_elig(other.artifact_hash or "", "3.0.0"), actor="owner")
    with pytest.raises(Refused, match="another model"):
        _load(reg, loader, module, version="3.0.0")


def test_revoked_approval_is_refused(conn: sqlite3.Connection, pkg: Path) -> None:
    reg, loader, module = _publish(conn, pkg)
    manifest = reg.get("model", "toy", "1.0.0")
    assert manifest is not None
    revoked = _elig(manifest.artifact_hash, user_enabled=False, eligibility=Eligibility.DISABLED,
                    approved_modes=[], evidence_refs=[], revoked_at=T0)  # fmt: skip
    reg.set_eligibility(revoked, actor="owner")
    with pytest.raises(Refused):
        _load(reg, loader, module)


def test_expiry_is_judged_at_the_callers_now(conn: sqlite3.Connection, pkg: Path) -> None:
    reg, loader, module = _publish(conn, pkg, expires_at=datetime(2099, 1, 1, tzinfo=UTC))
    assert _load(reg, loader, module)
    with pytest.raises(Refused):
        _load(reg, loader, module, now=datetime(2099, 1, 2, tzinfo=UTC))


def test_the_baseline_needs_no_artifact_but_never_runs_live() -> None:
    assert load_baseline(BASELINE, mode=Mode.SHADOW).card == BASELINE.card
    with pytest.raises(Refused, match="LIVE"):
        load_baseline(BASELINE, mode=Mode.LIVE)


def test_predict_cannot_skip_the_gate() -> None:
    with pytest.raises(Refused):
        predict(BASELINE, _snapshot(), now=NOW)  # type: ignore[arg-type]
    with pytest.raises(Refused):
        LoadedPredictor(BASELINE, Mode.DEMO, object())


# --- prediction ---------------------------------------------------------------------------------


def test_prediction_carries_lineage_semantics_and_validity(
    conn: sqlite3.Connection, pkg: Path
) -> None:
    reg, loader, module = _publish(conn, pkg)
    loaded = _load(reg, loader, module)
    p = predict(loaded, _snapshot(), now=NOW)
    assert p.direction is Direction.UP and p.probability.value == D("0.6")
    assert p.lineage is ForecastLineage.CHART_ONLY and p.probability.calibrated is False
    assert "score for the predicted direction" in p.probability.semantics
    assert p.valid_until == T0 + timedelta(minutes=5)
    assert p.input_refs == [f"m02.features.v1@{T0.isoformat()}"]
    down = predict(loaded, _snapshot(**{"H1.rsi_14.rsi": D(40)}), now=NOW)
    assert down.direction is Direction.DOWN and down.probability.value == D("0.6")


def test_news_inputs_are_refused_until_news_features_exist(
    conn: sqlite3.Connection, pkg: Path
) -> None:
    reg, loader, module = _publish(conn, pkg)
    with pytest.raises(Refused, match="chart-only"):
        predict(_load(reg, loader, module), _snapshot(), now=NOW, news_assessment_ids=["news-1"])


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"H1.atr_14.atr": None}, "MISSING_FEATURE"),
        ({"H1.atr_14.atr": D(80)}, "OUT_OF_DISTRIBUTION"),
    ],
)
def test_bad_features_are_refused(
    conn: sqlite3.Connection, pkg: Path, values: dict[str, Decimal | None], reason: str
) -> None:
    reg, loader, module = _publish(conn, pkg)
    with pytest.raises(Refused, match=reason):
        predict(_load(reg, loader, module), _snapshot(**values), now=NOW)


@pytest.mark.parametrize(
    "change",
    [{"quality": DataQuality.STALE}, {"quality": DataQuality.MISSING},
     {"feature_schema": "m02.features.v9"}],
)  # fmt: skip
def test_unusable_snapshots_are_refused(change: dict[str, object]) -> None:
    base = load_baseline(BASELINE, mode=Mode.DEMO)
    with pytest.raises(Refused):
        predict(base, _snapshot().model_copy(update=change), now=NOW)


def test_expired_or_future_snapshot_is_refused() -> None:
    base = load_baseline(BASELINE, mode=Mode.DEMO)
    with pytest.raises(Refused):
        predict(base, _snapshot(), now=T0 + timedelta(hours=1))
    with pytest.raises(Refused):
        predict(base, _snapshot(), now=T0)  # not yet available


def test_horizon_is_measured_from_the_snapshot_bar() -> None:
    base = load_baseline(BASELINE, mode=Mode.DEMO)
    long_lived = _snapshot().model_copy(update={"valid_until": T0 + timedelta(hours=5)})
    p = predict(base, long_lived, now=T0 + timedelta(minutes=40))
    assert p.valid_until == T0 + timedelta(hours=1)  # one H1 bar after as_of, not after now
    with pytest.raises(Refused, match="horizon"):
        predict(base, long_lived, now=T0 + timedelta(hours=2))


def test_baseline_follows_the_h1_trend_and_says_it_is_a_baseline() -> None:
    base = load_baseline(BASELINE, mode=Mode.DEMO)
    p = predict(base, _snapshot(), now=NOW)
    assert p.direction is Direction.UP and p.probability.value == D("0.5")
    assert "baseline" in p.probability.semantics and not p.probability.calibrated
    flat = predict(base, _snapshot(**{"H1.direction_10.direction": D(0)}), now=NOW)
    assert flat.direction is Direction.NEUTRAL


@pytest.mark.parametrize(
    "body",
    [
        "return {Direction.UP: D('0.5'), Direction.DOWN: D('0.5')}",  # tie: no signal
        "return {Direction.UP: D('NaN')}",
        "return {Direction.UP: 0.7}",  # a float
        "raise RuntimeError('boom')",
        "return {Direction.UP: D('1.5')}",
    ],
)
def test_bad_model_output_is_refused_not_crashed(
    conn: sqlite3.Connection, pkg: Path, body: str
) -> None:
    reg, loader, module = _publish(conn, pkg, version="2.0.0", body=body)
    loaded = _load(reg, loader, module, version="2.0.0")
    with pytest.raises(Refused):
        predict(loaded, _snapshot(), now=NOW)


def test_card_needs_evidence_to_claim_calibration() -> None:
    with pytest.raises(ValueError):
        _card(calibrated=True, evidence_refs=[])


class _Impostor:
    """Looks like a LoadedPredictor but never went through the gate."""

    predictor = BASELINE
    card = BASELINE.card
    mode = Mode.DEMO


def test_a_lookalike_of_a_loaded_predictor_is_refused() -> None:
    with pytest.raises(Refused, match="loaded predictor"):
        predict(_Impostor(), _snapshot(), now=NOW)  # type: ignore[arg-type]
