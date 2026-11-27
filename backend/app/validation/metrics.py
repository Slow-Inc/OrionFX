"""Validation metrics (#66; VAL-02, VAL-04; master plan §11.1).

Pure Decimal functions. Each returns its sample size `n` with the value, and an empty
sample gives `value=None`, never 0: "no evidence" must not read as "zero error" or
"zero loss".
- Probabilities are of the predicted event (e.g. P(close up)); outcomes are whether
  it happened.
- Trade results are net P&L after costs, in account currency, in time order.
- Paired inputs of different lengths raise ValueError (`zip(strict=True)`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Direction

D = Decimal


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    n: int


@dataclass(frozen=True)
class ReliabilityBin:
    low: Decimal
    high: Decimal
    n: int
    mean_probability: Decimal | None
    frequency: Decimal | None


def _mean(xs: Sequence[Decimal]) -> Metric:
    return Metric(sum(xs, D(0)) / len(xs) if xs else None, len(xs))


def direction_accuracy(predicted: Sequence[Direction], actual: Sequence[Direction]) -> Metric:
    """Share of correct calls among outcomes that moved (a NEUTRAL outcome is skipped)."""
    decided = [(p, a) for p, a in zip(predicted, actual, strict=True) if a is not Direction.NEUTRAL]
    hits = [D(int(p is a)) for p, a in decided]
    return _mean(hits)


def brier(probabilities: Sequence[Decimal], outcomes: Sequence[bool]) -> Metric:
    return _mean([(p - D(int(o))) ** 2 for p, o in zip(probabilities, outcomes, strict=True)])


def reliability(
    probabilities: Sequence[Decimal], outcomes: Sequence[bool], *, bins: int = 10
) -> list[ReliabilityBin]:
    """Equal-width bins over [0, 1]: forecast vs observed frequency (calibration)."""
    width = D(1) / bins
    out: list[ReliabilityBin] = []
    for i in range(bins):
        low, high = width * i, width * (i + 1)
        last = i == bins - 1
        members = [(p, o) for p, o in zip(probabilities, outcomes, strict=True)
                   if low <= p < high or (last and p == high)]  # fmt: skip
        n = len(members)
        out.append(ReliabilityBin(
            low, high, n,
            sum((p for p, _ in members), D(0)) / n if n else None,
            D(sum(o for _, o in members)) / n if n else None,
        ))  # fmt: skip
    return out


def mae(predicted: Sequence[Decimal], actual: Sequence[Decimal]) -> Metric:
    return _mean([abs(p - a) for p, a in zip(predicted, actual, strict=True)])


def rmse(predicted: Sequence[Decimal], actual: Sequence[Decimal]) -> Metric:
    m = _mean([(p - a) ** 2 for p, a in zip(predicted, actual, strict=True)])
    return Metric(None if m.value is None else m.value.sqrt(), m.n)


def expectancy(trades: Sequence[Decimal]) -> Metric:
    """Mean net result per trade, after costs."""
    return _mean(list(trades))


def win_rate(trades: Sequence[Decimal]) -> Metric:
    return _mean([D(int(t > 0)) for t in trades])


def profit_factor(trades: Sequence[Decimal]) -> Metric:
    """Gross profit / gross loss; None when there is no loss (undefined, not infinite)."""
    gains = sum((t for t in trades if t > 0), D(0))
    losses = -sum((t for t in trades if t < 0), D(0))
    return Metric(gains / losses if losses else None, len(trades))


def max_drawdown(trades: Sequence[Decimal]) -> Metric:
    """Deepest peak-to-trough fall of the cumulative result, starting from 0."""
    equity = peak = deepest = D(0)
    for t in trades:
        equity += t
        peak = max(peak, equity)
        deepest = max(deepest, peak - equity)
    return Metric(deepest if trades else None, len(trades))


def target_hit_rate(hits: Sequence[bool]) -> Metric:
    """Share of cycles that achieved their Profit Target."""
    return _mean([D(int(h)) for h in hits])
