"""The trusted LightGBM adapter (#130): hash-verified native boosters, the seed mean,
temperature-scaled, refused on any schema, label, seed or config mismatch, and served
through both the live load (approval required) and the evaluation load."""

import json
import pickle
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.support import training_rows
from tests.support.training_rows import FEATURES

from app.core.contracts import EligibilityRecord, FeatureSnapshot, sha256_ref
from app.core.enums import DataQuality, Direction, Eligibility, Mode
from app.learning.trainer import ADAPTER_MODULE, TrainedModel
from app.prediction.adapters import lightgbm_native
from app.prediction.card import ModelCard
from app.prediction.engine import (
    Refused,
    load_for_evaluation,
    load_model,
    predict,
    predict_for_evaluation,
)
from app.prediction.transforms import TRANSFORM_SCHEMA
from app.storage.artifacts import ArtifactLoader
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

X = {FEATURES[0]: Decimal("1.2"), FEATURES[1]: Decimal("-0.3")}
XS = np.array([[1.2, -0.3]])


def _model() -> TrainedModel:
    return training_rows.trained()


def _card(model: TrainedModel | None = None, **over: Any) -> ModelCard:
    card = (model or _model()).card(model_id="m04-lgbm", model_version="1",
                                    evidence_refs=["dataset:synthetic@1"])  # fmt: skip
    return card.model_copy(update=over)


def _repinned(edit: Callable[[dict[str, Any]], None], **card_over: Any) -> tuple[ModelCard, bytes]:
    """Weights edited by `edit`, with a card that pins the edited bytes: what is left to
    refuse them is the adapter's own checks, not the hash."""
    bundle = json.loads(_model().weights)
    edit(bundle)
    weights = json.dumps(bundle, sort_keys=True).encode()
    return _card(weights_hash=sha256_ref(weights), **card_over), weights


def _triple(scores: dict[Direction, Decimal]) -> np.ndarray:
    return np.array([float(scores[d]) for d in (Direction.UP, Direction.DOWN, Direction.NEUTRAL)])


def test_it_serves_the_trainers_probabilities_exactly_summing_to_one() -> None:
    scores = lightgbm_native.build(_card(), _model().weights).predict(X)
    assert set(scores) == set(Direction)
    assert all(isinstance(v, Decimal) and 0 <= v <= 1 for v in scores.values())
    assert abs(sum(scores.values()) - 1) < Decimal("1e-9")
    assert np.allclose(_triple(scores), _model().predict_proba(XS)[0], atol=1e-12)


def test_the_temperature_is_applied_and_every_seed_is_averaged() -> None:
    model = lightgbm_native.build(_card(), _model().weights)
    served = _triple(model.predict(X))
    seed_probs = np.stack([np.asarray(b.predict(XS)) for b in model.boosters])
    assert model.temperature != 1
    assert np.allclose(served, lightgbm_native.calibrate(seed_probs, model.temperature)[0])
    assert not np.allclose(served, lightgbm_native.calibrate(seed_probs, 1.0)[0])
    assert not np.allclose(served, lightgbm_native.calibrate(seed_probs[:1], model.temperature)[0])


def test_weights_that_are_not_the_pinned_ones_are_refused() -> None:
    tampered = _model().weights.replace(b'"temperature":"', b'"temperature":"1')
    with pytest.raises(ValueError, match="pins"):
        lightgbm_native.build(_card(), tampered)


def test_a_booster_that_is_not_the_one_its_hash_names_is_refused() -> None:
    def swap(b: dict[str, Any]) -> None:
        b["boosters"][0]["model"] = b["boosters"][1]["model"]

    card, weights = _repinned(swap)
    with pytest.raises(ValueError, match="booster seed 1"):
        lightgbm_native.build(card, weights)


@pytest.mark.parametrize(("field", "value", "match"), [
    ("label_version", "m07.labels.tb.v1", "label"),
    ("feature_schema", "m02.features.v2", "feature schema"),
    ("features", list(reversed(FEATURES)), "features"),
    ("classes", ["DOWN", "UP", "FLAT"], "class order"),
    ("training_cutoff", "2026-01-01T00:00:00+00:00", "cutoff"),
    ("temperature", "-1", "temperature"),
    ("format", "m04.lightgbm.v0", "m04.lightgbm.v1"),
])  # fmt: skip
def test_weights_disagreeing_with_the_card_are_refused(field: str, value: Any, match: str
                                                       ) -> None:  # fmt: skip
    card, weights = _repinned(lambda b: b.update({field: value}))
    with pytest.raises(ValueError, match=match):
        lightgbm_native.build(card, weights)


def test_a_card_on_another_label_or_feature_schema_is_refused() -> None:
    for over, match in (({"label_version": "m07.labels.tb.v1"}, "label"),
                        ({"feature_schema": "m02.features.v2"}, "feature schema")):  # fmt: skip
        with pytest.raises(ValueError, match=match):
            lightgbm_native.build(_card(**over), _model().weights)


def test_seeds_or_config_not_matching_the_cards_record_are_refused() -> None:
    def drop_seed(b: dict[str, Any]) -> None:
        b["boosters"] = b["boosters"][:1]

    card, weights = _repinned(drop_seed)
    with pytest.raises(ValueError, match="one booster per seed"):
        lightgbm_native.build(card, weights)
    card, weights = _repinned(lambda b: b["config"].update({"num_leaves": 31}))
    with pytest.raises(ValueError, match="config"):
        lightgbm_native.build(card, weights)
    with pytest.raises(ValueError, match="training record"):
        lightgbm_native.build(_card(training=None), _model().weights)


def test_pickled_weights_are_never_unpickled() -> None:
    weights = pickle.dumps({"boosters": []})
    with pytest.raises(ValueError):  # the hash matches; the bytes are not JSON
        lightgbm_native.build(_card(weights_hash=sha256_ref(weights)), weights)


# --- the engine's two load paths -------------------------------------------------------------

T0 = datetime(2026, 3, 2, 12, tzinfo=UTC)


def _snapshot(as_of: datetime = T0) -> FeatureSnapshot:
    return FeatureSnapshot(symbol="XAUUSD", feature_schema=TRANSFORM_SCHEMA, values=dict(X),
                           quality=DataQuality.OK, as_of=as_of,
                           available_at=as_of + timedelta(seconds=1),
                           valid_until=as_of + timedelta(minutes=5))  # fmt: skip


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "lgbm.db")
    migrate(c)
    yield c
    c.close()


def _registered(conn: sqlite3.Connection, approve: bool) -> tuple[ArtifactRegistry, ArtifactLoader]:
    reg = ArtifactRegistry(conn, clock=lambda: T0 - timedelta(days=1))
    card = _card()
    reg.register("model", card.manifest(), actor="m07")
    if approve:
        reg.set_eligibility(EligibilityRecord(
            subject_hash=card.manifest().artifact_hash, subject_kind="model",
            subject_ref=card.ref, user_enabled=True, eligibility=Eligibility.SHADOW_ONLY,
            approved_modes=[Mode.SHADOW], context={}, evidence_refs=["test"],
            policy_version="test", expires_at=None, revoked_at=None,
        ), actor="owner")  # fmt: skip
    return reg, ArtifactLoader(reg, allowed_packages=("app.prediction.adapters",))


def _live(reg: ArtifactRegistry, loader: ArtifactLoader, **over: Any) -> Any:
    args: dict[str, Any] = {"model_id": "m04-lgbm", "version": "1", "module": ADAPTER_MODULE,
                            "mode": Mode.SHADOW, "now": T0, "card": _card(),
                            "weights": _model().weights}  # fmt: skip
    args.update(over)
    return load_model(loader, reg, **args)


def test_the_live_load_serves_it_only_with_an_approval(conn: sqlite3.Connection) -> None:
    reg, loader = _registered(conn, approve=True)
    p = predict(_live(reg, loader), _snapshot(), now=T0 + timedelta(seconds=2))
    c = p.class_probabilities
    assert c is not None and p.probability.calibrated
    assert np.allclose([float(c.up), float(c.down), float(c.flat)], _model().predict_proba(XS)[0])
    with pytest.raises(Refused, match="pins"):
        _live(reg, loader, weights=_model().weights + b" ")
    with pytest.raises(Refused, match="registered"):
        _live(reg, loader, card=_card(training_cutoff=T0))


def test_the_live_load_refuses_without_an_approval(conn: sqlite3.Connection) -> None:
    reg, loader = _registered(conn, approve=False)
    with pytest.raises(Refused, match="eligible"):
        _live(reg, loader)


def test_the_evaluation_load_serves_it_without_an_approval(conn: sqlite3.Connection) -> None:
    reg, loader = _registered(conn, approve=False)
    model = load_for_evaluation(loader, reg, card=_card(), weights=_model().weights,
                                module=ADAPTER_MODULE)  # fmt: skip
    p = predict_for_evaluation(model, _snapshot(), now=T0 + timedelta(seconds=2))
    assert p.class_probabilities is not None
    with pytest.raises(Refused, match="cutoff"):
        predict_for_evaluation(model, _snapshot(_model().training_cutoff),
                               now=_model().training_cutoff + timedelta(seconds=2))  # fmt: skip
