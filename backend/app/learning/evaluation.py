"""Per-fold model metrics for the three-class label (#130; training-loop.md §5 tier 1).

Offline, on floats. Probabilities are (rows, 3) in the label's class order (UP, DOWN,
FLAT), labels are class indices, weights are optional per-row weights (uniqueness).
- Brier: the multiclass Brier score, the squared error summed over classes, averaged.
- Brier skill: 1 - Brier / Brier of climatology, climatology being the class frequencies
  of the TRAINING fold only (`class_prior` on the training labels), never the test fold.
- Log loss, and per-class reliability on equal-mass bins.
- ECE: `binned_ece` (classwise, equal-mass bins) is the default estimator behind the
  `ece` argument. It is biased upward on small samples; the debiased estimator and where
  the gate applies (pooled or per fold) are pre-registered in the protocol (#132), which
  plugs its estimator in through that argument.
- Feature importance: MDA (permutation, scored by log loss) on a slice the model never
  trained or calibrated on; never MDI (split or gain counts), which is in-sample.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Probs = NDArray[np.float64]
Labels = NDArray[np.int64]
Weights = NDArray[np.float64] | None
K = 3
_FLOOR = 1e-15


def _w(y: Labels, w: Weights) -> NDArray[np.float64]:
    return np.ones(len(y)) if w is None else np.asarray(w, dtype=np.float64)


def _onehot(y: Labels) -> Probs:
    return np.eye(K)[y]


def class_prior(y: Labels, w: Weights = None) -> Probs:
    """The (weighted) class frequencies: the climatology forecast."""
    weights = _w(y, w)
    return np.asarray([weights[y == k].sum() for k in range(K)]) / weights.sum()


def brier(p: Probs, y: Labels, w: Weights = None) -> float:
    return float(np.average(((p - _onehot(y)) ** 2).sum(axis=1), weights=_w(y, w)))


def log_loss(p: Probs, y: Labels, w: Weights = None) -> float:
    picked = np.clip(p[np.arange(len(y)), y], _FLOOR, 1.0)
    return float(np.average(-np.log(picked), weights=_w(y, w)))


def brier_skill(p: Probs, y: Labels, prior: Probs, w: Weights = None) -> float:
    """1 - Brier(p) / Brier(climatology); `prior` from the training fold."""
    return 1.0 - brier(p, y, w) / brier(np.tile(prior, (len(y), 1)), y, w)


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    cls: int
    mean_predicted: float
    observed: float
    n: int


def reliability(p: Probs, y: Labels, bins: int = 10) -> list[ReliabilityBin]:
    """Per class: equal-mass bins of the predicted probability, each with its mean
    prediction and the observed frequency of that class."""
    out = []
    for k in range(K):
        order = np.argsort(p[:, k], kind="stable")
        for chunk in np.array_split(order, min(bins, len(order))):
            out.append(ReliabilityBin(k, float(p[chunk, k].mean()),
                                      float((y[chunk] == k).mean()), len(chunk)))  # fmt: skip
    return out


def binned_ece(p: Probs, y: Labels, bins: int = 10) -> float:
    """Classwise ECE on equal-mass bins: the n-weighted mean |predicted - observed| over
    every class's bins, averaged over classes. Biased upward on small samples (#132)."""
    rel = reliability(p, y, bins)
    return sum(b.n * abs(b.mean_predicted - b.observed) for b in rel) / (K * len(y))


EceEstimator = Callable[[Probs, Labels], float]


@dataclass(frozen=True, slots=True)
class FoldReport:
    n: int
    n_eff: float
    """Sum of the uniqueness weights: about n / label concurrency."""
    brier: float
    baseline_brier: float
    brier_skill: float
    log_loss: float
    ece: float
    reliability: list[ReliabilityBin]


def fold_report(
    p: Probs, y: Labels, train_prior: Probs, w: Weights = None, *, ece: EceEstimator = binned_ece
) -> FoldReport:
    """One fold's metrics against the training fold's climatology."""
    b, base = brier(p, y, w), brier(np.tile(train_prior, (len(y), 1)), y, w)
    return FoldReport(
        n=len(y), n_eff=float(_w(y, w).sum()), brier=b, baseline_brier=base,
        brier_skill=1 - b / base, log_loss=log_loss(p, y, w), ece=ece(p, y),
        reliability=reliability(p, y),
    )  # fmt: skip


def mda(
    predict: Callable[[NDArray[np.float64]], Probs],
    x: NDArray[np.float64],
    y: Labels,
    features: Sequence[str],
    *,
    w: Weights = None,
    repeats: int = 5,
    seed: int = 0,
) -> dict[str, float]:
    """Mean decrease in accuracy, as the mean rise in log loss when one feature's column
    is permuted. Run it on an out-of-sample, purged slice."""
    rng = np.random.default_rng(seed)
    base = log_loss(predict(x), y, w)
    out = {}
    for j, name in enumerate(features):
        rises = []
        for _ in range(repeats):
            shuffled = x.copy()
            shuffled[:, j] = rng.permutation(shuffled[:, j])
            rises.append(log_loss(predict(shuffled), y, w) - base)
        out[name] = float(np.mean(rises))
    return out
