"""Independent recomputation of a run's headline metrics (#132; training-loop.md §6 rule 6).

A second implementation, deliberately written apart from whatever scorer produced the
logged numbers: it imports no scorer, only the per-prediction outputs (class
probabilities and the true class index) that the run stored. `recheck` compares its
numbers with the logged ones and flags every mismatch beyond an absolute tolerance.

Recomputed metrics:
- `brier`: multiclass Brier, the mean over predictions of sum_k (p_k - y_k)^2;
- `accuracy`: share of predictions whose highest probability is the true class (a tie
  goes to the lowest class index);
- `log_loss`: mean of -ln p_true (a zero p_true gives infinity, not a clip);
- `brier_skill`: 1 - sum Brier(model) / sum Brier(prior), only when the training prior
  is given.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Prediction:
    probabilities: tuple[float, ...]
    label: int


@dataclass(frozen=True)
class Recheck:
    recomputed: dict[str, float]
    checked: list[str]
    unchecked: list[str]
    """Logged metrics this checker cannot recompute from predictions alone."""
    mismatches: dict[str, tuple[float, float]]
    """metric -> (logged, recomputed)."""

    @property
    def ok(self) -> bool:
        return bool(self.checked) and not self.mismatches


def _valid(probabilities: Sequence[float]) -> None:
    if any(not 0 <= p <= 1 for p in probabilities) or abs(sum(probabilities) - 1) > 1e-6:
        raise ValueError("class probabilities must lie in [0, 1] and sum to 1")


def _brier(probabilities: Sequence[float], label: int) -> float:
    return sum((p - (k == label)) ** 2 for k, p in enumerate(probabilities))


def recompute(
    predictions: Sequence[Prediction], *, prior: Sequence[float] | None = None
) -> dict[str, float]:
    if not predictions:
        raise ValueError("no predictions to recompute from")
    width = len(predictions[0].probabilities)
    for row in predictions:
        if len(row.probabilities) != width or not 0 <= row.label < width:
            raise ValueError("every prediction needs the same classes and a label among them")
        _valid(row.probabilities)
    n = len(predictions)
    brier_sum = sum(_brier(r.probabilities, r.label) for r in predictions)
    hits = sum(max(range(width), key=lambda k: r.probabilities[k]) == r.label for r in predictions)
    p_true = [r.probabilities[r.label] for r in predictions]
    out = {
        "brier": brier_sum / n,
        "accuracy": hits / n,
        "log_loss": math.inf if 0 in p_true else -sum(math.log(p) for p in p_true) / n,
    }
    if prior is not None:
        if len(prior) != width:
            raise ValueError("the prior must cover the same classes")
        _valid(prior)
        reference = sum(_brier(prior, r.label) for r in predictions)
        out["brier_skill"] = 1 - brier_sum / reference
    return out


def recheck(
    predictions: Sequence[Prediction],
    logged: Mapping[str, float],
    *,
    prior: Sequence[float] | None = None,
    tolerance: float = 1e-9,
) -> Recheck:
    """Recompute and compare with the logged metrics."""
    ours = recompute(predictions, prior=prior)
    checked = sorted(k for k in logged if k in ours)
    mismatches = {
        k: (logged[k], ours[k]) for k in checked if not abs(logged[k] - ours[k]) <= tolerance
    }
    return Recheck(ours, checked, sorted(k for k in logged if k not in ours), mismatches)
