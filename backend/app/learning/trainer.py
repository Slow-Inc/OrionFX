"""The M04 LightGBM trainer (#130, PRD #120 slice 3). Offline, on floats; nothing here
trades or touches the runtime.

Input: the rows of one `export_dataset(..., transforms=True)` file (M04 transforms under
TRANSFORM_SCHEMA, `m07.labels.tb.v2` barrier labels, splits in time order). Output: a
`TrainedModel` holding the native weights file for the `lightgbm_native` adapter and what
its card states.

- **Fit** on the `train` split: LightGBM `multiclass` (softmax over UP, DOWN, FLAT),
  shallow and strongly regularised (`LightGBMConfig`), one booster per seed, the seeds
  averaged by the adapter. Rows are weighted by their label's average uniqueness (the
  mean of 1/concurrency over its span, as_of to the label's `available_at`). No class
  rebalancing: reweighting classes would bend the probabilities EV prices.
- **Early stopping** on the last `inner_validation_fraction` of the train split by time;
  the rows before it whose label was not yet known when it starts are purged. The
  early-stopped booster is kept (no refit).
- **Calibration**: one temperature, fitted by weighted log loss on the `validation` split,
  which the export already purged and which sits immediately before the test window,
  through the adapter's own `calibrate`. Isotonic is not built: it needs an effective
  sample of about 10k in the calibration slice, which no slice has yet.
- **Training cutoff**: the latest `available_at` of every label read (train and
  validation), so the engine refuses to score any snapshot at or before it.
- **Leak checks**, before fitting: every feature must be an M04 transform key, and no
  single feature may predict the label almost perfectly on the train split
  (`LeakSuspected`), which a feature built from the label would.
- **Reproducible**: `deterministic` LightGBM with fixed seeds; the config is a frozen,
  typed object whose canonical-JSON hash goes into the card's `TrainingRecord`.

Nothing is pickled: boosters leave as `model_to_string` text, and the probabilities this
module reports are computed from boosters re-parsed from that text, exactly as served.
"""

import functools
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray
from pydantic import Field

from app.core.bet import LABEL_VERSION
from app.core.contracts import ContractModel, ParameterBound, canonical_json, sha256_ref
from app.core.enums import ForecastLineage, Timeframe
from app.learning.bars import Horizon
from app.learning.evaluation import class_prior, fold_report, log_loss
from app.learning.export import horizon_key
from app.prediction.adapters import lightgbm_native as adapter
from app.prediction.card import BASELINE_BRIER, BRIER, ModelCard, TrainingRecord
from app.prediction.transforms import TRANSFORM_SCHEMA, transform_keys

__all__ = [
    "ADAPTER_MODULE",
    "Examples",
    "LeakSuspected",
    "LightGBMConfig",
    "TrainedModel",
    "adapter_hash",
    "examples",
    "train",
    "uniqueness",
]

ADAPTER_MODULE = "app.prediction.adapters.lightgbm_native"
LEAK_SKILL = 0.9
"""Share of the error left by the class prior that one feature alone may not remove."""
_LEAK_BINS = 16


@functools.cache
def adapter_hash() -> str:
    """The registered artifact hash of every LightGBM model: the adapter's source."""
    return sha256_ref(Path(adapter.__file__).read_bytes())


class LeakSuspected(ValueError):
    """A feature that should not be there, or that predicts the label too well."""


class LightGBMConfig(ContractModel):
    """One training configuration. Defaults are the research note's pre-registration
    candidates for real data; the tracer passes a tiny one."""

    features: list[str] = Field(min_length=1)
    horizon: Timeframe
    horizon_bars: int = Field(gt=0)
    seeds: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5], min_length=1)
    num_leaves: int = 15
    max_depth: int = 4
    min_data_in_leaf: int = 500
    learning_rate: float = 0.02
    feature_fraction: float = 0.7
    bagging_fraction: float = 0.7
    bagging_freq: int = 1
    lambda_l2: float = 10.0
    max_bin: int = 63
    num_boost_round: int = 2000
    early_stopping_rounds: int = 100
    inner_validation_fraction: float = Field(default=0.15, gt=0, lt=1)
    calibrator: Literal["temperature"] = "temperature"

    @property
    def label(self) -> str:
        return horizon_key(Horizon(self.horizon, self.horizon_bars))

    @property
    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def params(self, seed: int) -> dict[str, Any]:
        return {
            "objective": "multiclass", "num_class": len(adapter.CLASSES),
            "num_leaves": self.num_leaves, "max_depth": self.max_depth,
            "min_data_in_leaf": self.min_data_in_leaf, "learning_rate": self.learning_rate,
            "feature_fraction": self.feature_fraction,
            "bagging_fraction": self.bagging_fraction, "bagging_freq": self.bagging_freq,
            "lambda_l2": self.lambda_l2, "max_bin": self.max_bin, "seed": seed,
            "deterministic": True, "force_row_wise": True, "metric": "multi_logloss",
            "verbose": -1,
        }  # fmt: skip


@dataclass(frozen=True)
class Examples:
    """One split's usable rows, in time order."""

    x: NDArray[np.float64]
    y: NDArray[np.int64]
    start: NDArray[np.float64]
    """as_of, epoch seconds."""
    end: NDArray[np.float64]
    """The label's available_at, epoch seconds."""

    def __len__(self) -> int:
        return len(self.y)

    def take(self, mask: NDArray[np.bool_]) -> "Examples":
        return Examples(self.x[mask], self.y[mask], self.start[mask], self.end[mask])


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def examples(rows: list[dict[str, Any]], config: LightGBMConfig, split: str) -> Examples:
    """The `split` rows with every feature and a barrier label; refuses rows of another
    feature schema or label version."""
    xs, ys, starts, ends = [], [], [], []
    for r in rows:
        if r["split"] != split:
            continue
        if r["feature_schema"] != TRANSFORM_SCHEMA:
            raise ValueError(f"rows are {r['feature_schema']}, not {TRANSFORM_SCHEMA}")
        label = r["barrier_labels"].get(config.label)
        values = [r["features"].get(k) for k in config.features]
        if label is None or None in values:
            continue
        if label["label_version"] != LABEL_VERSION:
            raise ValueError(f"rows are labelled {label['label_version']}, not {LABEL_VERSION}")
        xs.append([float(v) for v in values])
        ys.append(adapter.CLASSES.index(label["outcome"]))
        starts.append(_epoch(r["as_of"]))
        ends.append(_epoch(label["available_at"]))
    order = np.argsort(np.asarray(starts), kind="stable")
    return Examples(np.asarray(xs, dtype=np.float64).reshape(len(ys), len(config.features))[order],
                    np.asarray(ys, dtype=np.int64)[order], np.asarray(starts)[order],
                    np.asarray(ends)[order])  # fmt: skip


def uniqueness(start: NDArray[np.float64], end: NDArray[np.float64]) -> NDArray[np.float64]:
    """Each label's average uniqueness: the mean of 1/concurrency over the sample times
    its span [start, end) covers, concurrency being how many spans cover that time."""
    grid = np.unique(start)
    lo = np.searchsorted(grid, start, side="left")
    hi = np.maximum(np.searchsorted(grid, end, side="left"), lo + 1)
    delta = np.zeros(len(grid) + 1)
    np.add.at(delta, lo, 1)
    np.add.at(delta, hi, -1)
    inverse = np.concatenate([[0.0], np.cumsum(1.0 / np.cumsum(delta)[:-1])])
    out: NDArray[np.float64] = (inverse[hi] - inverse[lo]) / (hi - lo)
    return out


def _check_leaks(train: Examples, config: LightGBMConfig) -> None:
    known = set(transform_keys(Timeframe))
    unknown = [f for f in config.features if f not in known]
    if unknown:
        raise LeakSuspected(f"not M04 transform features: {unknown}")
    prior = np.bincount(train.y, minlength=len(adapter.CLASSES)).max() / len(train)
    if prior >= 1:
        return
    for j, name in enumerate(config.features):
        col = train.x[:, j]
        edges = np.unique(np.quantile(col, np.linspace(0, 1, _LEAK_BINS + 1)[1:-1]))
        bins = np.searchsorted(edges, col, side="right")
        hits = sum(np.bincount(train.y[bins == b]).max() for b in np.unique(bins))
        skill = (hits / len(train) - prior) / (1 - prior)
        if skill >= LEAK_SKILL:
            raise LeakSuspected(f"{name} alone predicts the label (skill {skill:.2f}):"
                                " a feature built from the future?")  # fmt: skip


def _inner_split(train: Examples, fraction: float) -> tuple[Examples, Examples]:
    """(fit, early-stop) by time; fit rows whose label is known only after the early-stop
    slice starts are purged."""
    cut = train.start[int(len(train) * (1 - fraction))]
    fit = train.take((train.start < cut) & (train.end <= cut))
    stop = train.take(train.start >= cut)
    if not len(fit) or not len(stop):
        raise ValueError("too few train rows for a purged early-stopping slice")
    return fit, stop


def _fit_temperature(seed_probs: NDArray[np.float64], y: NDArray[np.int64],
                     w: NDArray[np.float64]) -> float:  # fmt: skip
    """The temperature minimising weighted log loss; the loss is convex in 1/T, so a
    golden-section search over 1/T in [0.05, 20] finds it."""

    def loss(beta: float) -> float:
        return log_loss(adapter.calibrate(seed_probs, 1 / beta), y, w)

    lo, hi, g = 0.05, 20.0, (5**0.5 - 1) / 2
    for _ in range(50):
        a, b = hi - g * (hi - lo), lo + g * (hi - lo)
        lo, hi = (lo, b) if loss(a) < loss(b) else (a, hi)
    return 2 / (lo + hi)


def _dec(x: float) -> Decimal:
    return Decimal(f"{x:.6f}")


@dataclass(frozen=True)
class TrainedModel:
    config: LightGBMConfig
    weights: bytes
    """The adapter's weights file (canonical JSON)."""
    temperature: float
    training_cutoff: datetime
    training: TrainingRecord
    validation: dict[str, Decimal]
    """On the calibration slice (the temperature was fitted there): Brier, the Brier of
    the train split's class prior, log loss, and the effective sample size."""
    feature_ranges: dict[str, ParameterBound]
    """The observed range of each feature over the rows it was trained and calibrated on."""
    train_prior: NDArray[np.float64]
    boosters: tuple[lgb.Booster, ...]
    """Re-parsed from the weights' text, as the adapter serves them."""

    def predict_proba(self, x: NDArray[np.float64]) -> NDArray[np.float64]:
        return adapter.calibrate(adapter.seed_probs(self.boosters, x), self.temperature)

    def card(self, *, model_id: str, model_version: str, evidence_refs: list[str]) -> ModelCard:
        c = self.config
        return ModelCard(
            model_id=model_id, model_version=model_version, artifact_hash=adapter_hash(),
            lineage=ForecastLineage.CHART_ONLY, feature_schema=TRANSFORM_SCHEMA,
            required_features=c.features, feature_ranges=self.feature_ranges,
            horizon=c.horizon, horizon_bars=c.horizon_bars,
            semantics=f"P(UP, DOWN, FLAT) of {LABEL_VERSION} over {c.horizon_bars}"
                      f" {c.horizon.value} bar(s): LightGBM multiclass, the mean of"
                      f" {len(c.seeds)} seeds, temperature-scaled on the validation split",
            calibrated=True, validation=self.validation, evidence_refs=evidence_refs,
            label_version=LABEL_VERSION, training_cutoff=self.training_cutoff,
            weights_hash=sha256_ref(self.weights), training=self.training,
        )  # fmt: skip


def train(
    rows: list[dict[str, Any]],
    config: LightGBMConfig,
    *,
    dataset_ref: str,
    code_commit: str,
) -> TrainedModel:
    """Fit on `train`, early-stop on its purged tail, calibrate on `validation`."""
    tr, cal = examples(rows, config, "train"), examples(rows, config, "validation")
    if not len(tr) or not len(cal):
        raise ValueError("training needs labelled train and validation rows")
    _check_leaks(tr, config)
    fit, stop = _inner_split(tr, config.inner_validation_fraction)
    w_fit, w_stop = uniqueness(fit.start, fit.end), uniqueness(stop.start, stop.end)
    texts = []
    for seed in config.seeds:
        params = config.params(seed)
        data = lgb.Dataset(fit.x, fit.y, weight=w_fit, params=params)
        valid = lgb.Dataset(stop.x, stop.y, weight=w_stop, reference=data, params=params)
        booster = lgb.train(params, data, num_boost_round=config.num_boost_round,
                            valid_sets=[valid], callbacks=[lgb.early_stopping(
                                config.early_stopping_rounds, verbose=False)])  # fmt: skip
        texts.append(booster.model_to_string(num_iteration=booster.best_iteration))
    boosters = tuple(lgb.Booster(model_str=t) for t in texts)
    w_cal = uniqueness(cal.start, cal.end)
    cal_probs = adapter.seed_probs(boosters, cal.x)
    temperature = _fit_temperature(cal_probs, cal.y, w_cal)
    prior = class_prior(tr.y)
    report = fold_report(adapter.calibrate(cal_probs, temperature), cal.y, prior)
    cutoff = max(datetime.fromisoformat(r["barrier_labels"][config.label]["available_at"])
                 for r in rows if r["split"] in ("train", "validation")
                 and r["barrier_labels"].get(config.label) is not None)  # fmt: skip
    record = TrainingRecord(dataset_ref=dataset_ref, config_hash=adapter.config_hash(
        config.as_json), seeds=config.seeds, libraries={"lightgbm": lgb.__version__,
        "numpy": np.__version__}, code_commit=code_commit)  # fmt: skip
    bundle = {
        "format": adapter.FORMAT, "classes": list(adapter.CLASSES),
        "features": config.features, "feature_schema": TRANSFORM_SCHEMA,
        "label_version": LABEL_VERSION, "training_cutoff": cutoff.isoformat(),
        "training": record.model_dump(mode="json"), "config": config.as_json,
        "temperature": repr(temperature),
        "boosters": [{"seed": s, "sha256": sha256_ref(t.encode()), "model": t}
                     for s, t in zip(config.seeds, texts, strict=True)],
    }  # fmt: skip
    both = np.concatenate([tr.x, cal.x])
    return TrainedModel(
        config=config,
        weights=canonical_json(bundle),
        temperature=temperature, training_cutoff=cutoff, training=record,
        validation={BRIER: _dec(report.brier), BASELINE_BRIER: _dec(report.baseline_brier),
                    "log_loss": _dec(report.log_loss), "n_eff": _dec(float(w_cal.sum()))},
        feature_ranges={k: ParameterBound(low=_dec(float(both[:, j].min())) - Decimal("1e-6"),
                                          high=_dec(float(both[:, j].max())) + Decimal("1e-6"))
                        for j, k in enumerate(config.features)},
        train_prior=prior, boosters=boosters,
    )  # fmt: skip
