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


def _rate(flags: Sequence[bool]) -> Metric:
    """Share of True."""
    return Metric(D(sum(flags)) / len(flags) if flags else None, len(flags))


def direction_accuracy(predicted: Sequence[Direction], actual: Sequence[Direction]) -> Metric:
    """Share of correct calls among real calls on outcomes that moved. A NEUTRAL outcome
    is skipped, and so is a NEUTRAL prediction: abstaining is not a wrong call."""
    pairs = list(zip(predicted, actual, strict=True))
    called = [(p, a) for p, a in pairs if Direction.NEUTRAL not in (p, a)]
    return _rate([p is a for p, a in called])


def _probabilities(ps: Sequence[Decimal]) -> None:
    if any(not D(0) <= p <= D(1) for p in ps):
        raise ValueError("probabilities must lie in [0, 1]")


def brier(probabilities: Sequence[Decimal], outcomes: Sequence[bool]) -> Metric:
    _probabilities(probabilities)
    return _mean([(p - D(int(o))) ** 2 for p, o in zip(probabilities, outcomes, strict=True)])


def reliability(
    probabilities: Sequence[Decimal], outcomes: Sequence[bool], *, bins: int = 10
) -> list[ReliabilityBin]:
    """Equal-width bins over [0, 1]: forecast vs observed frequency (calibration). A
    forecast of exactly 1 falls in the last bin."""
    if bins < 1:
        raise ValueError("bins must be at least 1")
    _probabilities(probabilities)
    buckets: list[list[tuple[Decimal, bool]]] = [[] for _ in range(bins)]
    for p, o in zip(probabilities, outcomes, strict=True):
        buckets[min(int(p * bins), bins - 1)].append((p, o))
    return [
        ReliabilityBin(
            low=D(i) / bins,
            high=D(1) if i == bins - 1 else D(i + 1) / bins,
            n=len(b),
            mean_probability=_mean([p for p, _ in b]).value,
            frequency=_rate([o for _, o in b]).value,
        )
        for i, b in enumerate(buckets)
    ]


def mae(predicted: Sequence[Decimal], actual: Sequence[Decimal]) -> Metric:
    return _mean([abs(p - a) for p, a in zip(predicted, actual, strict=True)])


def rmse(predicted: Sequence[Decimal], actual: Sequence[Decimal]) -> Metric:
    m = _mean([(p - a) ** 2 for p, a in zip(predicted, actual, strict=True)])
    return Metric(None if m.value is None else m.value.sqrt(), m.n)


def expectancy(trades: Sequence[Decimal]) -> Metric:
    """Mean net result per trade, after costs."""
    return _mean(trades)


def win_rate(trades: Sequence[Decimal]) -> Metric:
    """Share of winning trades among decided ones: a break-even (0) trade is neither a
    win nor a loss, as in `profit_factor`."""
    return _rate([t > 0 for t in trades if t != 0])


def profit_factor(trades: Sequence[Decimal]) -> Metric:
    """Gross profit / gross loss; None when there is no loss (undefined, not infinite)."""
    gains = sum((t for t in trades if t > 0), D(0))
    losses = -sum((t for t in trades if t < 0), D(0))
    return Metric(gains / losses if losses else None, len(trades))


def max_drawdown(trades: Sequence[Decimal]) -> Metric:
    """Deepest peak-to-trough fall of the cumulative trade result, starting from 0, in
    account currency. This is a backtest amount, not the Account DD percent M06 guards."""
    equity = peak = deepest = D(0)
    for t in trades:
        equity += t
        peak = max(peak, equity)
        deepest = max(deepest, peak - equity)
    return Metric(deepest if trades else None, len(trades))


def target_hit_rate(hits: Sequence[bool]) -> Metric:
    """Share of cycles that achieved their Profit Target."""
    return _rate(list(hits))
