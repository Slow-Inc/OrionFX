"""The M04 LightGBM trainer (#130): multiclass on the train split, purged early stopping,
seed averaging, temperature on the later validation split, a native artifact whose card
records how it was made, and leak checks before anything fits."""

import ast
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pytest
from tests.support import training_rows
from tests.support.training_rows import FEATURES, TINY

from app.core.bet import LABEL_VERSION
from app.learning import trainer
from app.learning.evaluation import class_prior, fold_report, mda
from app.learning.trainer import LeakSuspected, TrainedModel, examples, train, uniqueness
from app.prediction.adapters import lightgbm_native
from app.prediction.transforms import TRANSFORM_SCHEMA

D_REF = training_rows.DATASET_REF


def _trained() -> TrainedModel:
    return training_rows.trained()


def _bundle(model: TrainedModel) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(model.weights)
    return result


def test_it_learns_a_calibrated_three_class_model_that_beats_the_train_prior() -> None:
    model = _trained()
    card = model.card(model_id="m04-lgbm", model_version="1", evidence_refs=[f"dataset:{D_REF}"])
    assert card.calibrated and card.skill > 0
    assert card.label_version == LABEL_VERSION and card.feature_schema == TRANSFORM_SCHEMA
    assert card.required_features == FEATURES and card.artifact_hash == trainer.adapter_hash()
    p = model.predict_proba(np.array([[2.0, 0.0], [-2.0, 0.0]]))
    assert p[0, 0] > p[0, 1] and p[1, 1] > p[1, 0]
    assert np.allclose(p.sum(axis=1), 1)


def test_the_card_records_dataset_config_seeds_libraries_and_commit() -> None:
    model = _trained()
    rec = model.card(model_id="m", model_version="1", evidence_refs=["x"]).training
    assert rec is not None
    assert rec.dataset_ref == D_REF and rec.seeds == TINY.seeds and rec.code_commit == "test"
    assert rec.libraries["lightgbm"] == lgb.__version__
    assert rec.config_hash == lightgbm_native.config_hash(TINY.as_json)
    other = TINY.model_copy(update={"num_leaves": 5})
    assert lightgbm_native.config_hash(other.as_json) != rec.config_hash


def test_boosters_are_native_text_one_per_seed_and_the_seeds_differ() -> None:
    boosters = _bundle(_trained())["boosters"]
    assert [b["seed"] for b in boosters] == TINY.seeds
    assert all(b["model"].startswith("tree\n") for b in boosters)
    assert len({b["model"] for b in boosters}) == len(boosters)


def test_the_temperature_is_fitted_and_is_not_one() -> None:
    model = _trained()
    assert model.temperature > 0 and abs(model.temperature - 1) > 0.01
    assert float(_bundle(model)["temperature"]) == model.temperature


def test_a_model_that_is_confidently_wrong_on_the_calibration_slice_is_refused() -> None:
    """The temperature would run to its search edge: no calibrated claim is possible."""
    wrong = np.tile([0.9, 0.05, 0.05], (1, 50, 1))
    y = np.ones(50, dtype=np.int64)
    with pytest.raises(ValueError, match="search edge"):
        trainer._fit_temperature(wrong, y, np.ones(50))


def test_the_training_cutoff_is_the_latest_label_read_in_train_or_validation() -> None:
    rows = training_rows.rows()
    known = [datetime.fromisoformat(r["barrier_labels"]["H1x1"]["available_at"]) for r in rows
             if r["split"] in ("train", "validation")]  # fmt: skip
    assert _trained().training_cutoff == max(known)


def test_training_is_reproducible() -> None:
    again = train(training_rows.rows(), TINY, dataset_ref=D_REF, code_commit="test")
    assert again.weights == _trained().weights


def test_nothing_after_the_validation_split_is_read() -> None:
    """Trailing only: rewriting every holdout row (labels and features) changes nothing."""
    rows = training_rows.rows()
    for r in rows:
        if r["split"] == "holdout":
            r["features"] = {k: "9.0" for k in FEATURES}
            r["barrier_labels"]["H1x1"]["outcome"] = "UP"
    model = train(rows, TINY, dataset_ref=D_REF, code_commit="test")
    assert model.weights == _trained().weights
    assert np.array_equal(model.train_prior, class_prior(examples(rows, TINY, "train").y))


def test_a_feature_built_from_the_label_is_refused() -> None:
    """The future-feature canary: a transform key overwritten with the label itself."""
    rows = training_rows.rows()
    for r in rows:
        r["features"]["M15.log_return"] = str(
            lightgbm_native.CLASSES.index(r["barrier_labels"]["H1x1"]["outcome"])
        )
    canary = TINY.model_copy(update={"features": [*FEATURES, "M15.log_return"]})
    with pytest.raises(LeakSuspected, match=r"M15\.log_return"):
        train(rows, canary, dataset_ref=D_REF, code_commit="test")


def test_a_feature_that_is_not_an_m04_transform_is_refused() -> None:
    rows = training_rows.rows()
    for r in rows:
        r["features"]["close_next"] = "1"
    with pytest.raises(LeakSuspected, match="close_next"):
        train(rows, TINY.model_copy(update={"features": [*FEATURES, "close_next"]}),
              dataset_ref=D_REF, code_commit="test")  # fmt: skip


def test_rows_of_another_label_or_feature_schema_are_refused() -> None:
    rows = training_rows.rows()
    rows[0]["barrier_labels"]["H1x1"]["label_version"] = "m07.labels.tb.v1"
    with pytest.raises(ValueError, match="labelled"):
        examples(rows, TINY, "train")
    rows = training_rows.rows()
    rows[0]["feature_schema"] = "m02.features.v2"
    with pytest.raises(ValueError, match="m02"):
        examples(rows, TINY, "train")


def test_uniqueness_is_one_over_concurrency_averaged_over_the_span() -> None:
    start = np.array([0.0, 10.0, 20.0, 30.0])
    alone = uniqueness(start, start + 5)
    assert np.allclose(alone, 1)
    # Each label spans two sample times; neighbours share one.
    chained = uniqueness(start, start + 15)
    assert np.allclose(chained, [0.75, 0.5, 0.5, 0.5])


def test_the_early_stopping_slice_is_purged_of_labels_still_open_at_its_start() -> None:
    tr = examples(training_rows.rows(), TINY, "train")
    fit, stop = trainer._inner_split(tr, 0.15)
    assert fit.end.max() <= stop.start.min()
    assert len(fit) + len(stop) < len(tr)  # some rows straddling the cut were dropped


def test_no_class_rebalancing() -> None:
    params = TINY.params(1)
    assert params["objective"] == "multiclass"
    assert not {"is_unbalance", "class_weight", "scale_pos_weight"} & set(params)


def test_fold_metrics_score_against_the_train_fold_prior_and_mda_finds_the_signal() -> None:
    model = _trained()
    hold = examples(training_rows.rows(), TINY, "holdout")
    report = fold_report(model.predict_proba(hold.x), hold.y, model.train_prior,
                         uniqueness(hold.start, hold.end))  # fmt: skip
    assert report.brier < report.baseline_brier and report.brier_skill > 0
    assert 0 <= report.ece < 0.2 and report.n == len(hold) and report.n_eff < report.n
    assert {b.cls for b in report.reliability} == {0, 1, 2}
    importance = mda(model.predict_proba, hold.x, hold.y, FEATURES)
    assert importance[FEATURES[0]] > 0.05 > abs(importance[FEATURES[1]])


def test_no_pickle_anywhere_in_training_or_loading() -> None:
    """Neither module imports a deserialiser that can run code, nor reads a booster from a
    file or pickle state: boosters come only from `model_str` text."""
    banned_modules = {"pickle", "joblib", "marshal", "dill", "shelve", "cloudpickle"}
    banned_names = {"model_file", "__setstate__", "__getstate__", "loads_pickle"}
    for module in (trainer, lightgbm_native):
        tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree)
                    if isinstance(n, ast.Import | ast.ImportFrom)
                    for a in (n.names if isinstance(n, ast.Import) else
                              [ast.alias(n.module or "")])}  # fmt: skip
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call)
                  for k in n.keywords if k.arg}  # fmt: skip
        assert not imported & banned_modules, module.__name__
        assert not names & banned_names, module.__name__
