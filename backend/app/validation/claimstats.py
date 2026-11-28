"""Statistics for the M04 claim protocol (#132; PRD #120; training-loop.md §6 rules 2-5).

Research statistics on evidence, not money, so they are floats (the Decimal `metrics`
module stays the scorer of record). Standard library only: `statistics.NormalDist` gives
Phi and its inverse. Every random function takes an explicit seed.

- **Block bootstrap** over trading days (rule 2): stationary (Politis & Romano 1994,
  geometric blocks with mean `block`) or circular. Units are days, so overlapping
  horizons inside a day are not counted as independent. Brier skill is a ratio, so each
  replicate recomputes it from day sums.
- **Sign test**, exact one-sided binomial: P(at least k of n folds positive | p = 1/2).
- **Holm** step-down adjusted p-values.
- **Deflated Sharpe** (Bailey & López de Prado 2014) and **minimum track record length**
  (Bailey & López de Prado 2012). SR is per period at T's frequency (not annualised);
  kurtosis is non-excess (3 for a normal).
- **PBO by CSCV** (Bailey, Borwein, López de Prado & Zhu 2017) on a T x N matrix: rows are
  time periods (e.g. days), columns are configs. The score is the Sharpe ratio. A split
  whose in-sample winner ranks at or below the out-of-sample median (logit <= 0) counts
  as overfit, which is the conservative side of the boundary.
- **ECE** of a binary event (P(event) vs whether it happened; for top-label ECE pass
  the top probability and whether the top class was right), on equal-mass bins:
  - `ece_binned`: the plug-in L1 ECE, biased upward (Roelofs et al. 2022);
  - `ece_debiased`: Kumar, Liang & Ma 2019 ("Verified Uncertainty Calibration"), the
    debiased L2 calibration error: each bin's squared gap minus its sampling variance
    ybar(1-ybar)/(n_b-1), clipped at 0 before the square root. Its scale is L2, not L1.
  - the noise floor: the ECE a perfectly calibrated model shows at `n_eff`, in closed form
    for the binned L1 estimator and by simulation for any estimator.
- **Power** by simulation: the share of simulated worlds, under an assumed true effect,
  in which a pass rule passes (rule 3).
- **A/A noise floor** from seed-only reruns, and **best-of-N** against a fresh baseline
  (rules 4-5).
"""

import math
import random
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations, pairwise
from typing import Literal

_PHI = statistics.NormalDist()
EULER_GAMMA = 0.5772156649015329

Scheme = Literal["stationary", "circular"]


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    """`math.inf` for a one-sided lower bound."""


# --- block bootstrap ---------------------------------------------------------------------


def _indices(n: int, block: int, rng: random.Random, scheme: Scheme) -> list[int]:
    out: list[int] = []
    if scheme == "circular":
        while len(out) < n:
            start = rng.randrange(n)
            out.extend((start + i) % n for i in range(block))
        return out[:n]
    i = rng.randrange(n)
    while len(out) < n:
        out.append(i)
        i = rng.randrange(n) if rng.random() < 1 / block else (i + 1) % n
    return out


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear interpolation between order statistics."""
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def bootstrap_ci[T](
    units: Sequence[T],
    statistic: Callable[[Sequence[T]], float],
    *,
    block: int,
    resamples: int,
    seed: int,
    level: float = 0.95,
    one_sided: bool = False,
    scheme: Scheme = "stationary",
) -> Interval:
    """Percentile CI of `statistic` over block resamples of `units` (one unit per day)."""
    if not units:
        raise ValueError("no units to resample")
    if block < 1 or resamples < 1 or not 0 < level < 1:
        raise ValueError("block and resamples must be positive and level in (0, 1)")
    rng = random.Random(seed)
    n = len(units)
    reps = sorted(
        statistic([units[i] for i in _indices(n, block, rng, scheme)]) for _ in range(resamples)
    )
    point = statistic(units)
    if one_sided:
        return Interval(point, _quantile(reps, 1 - level), math.inf)
    tail = (1 - level) / 2
    return Interval(point, _quantile(reps, tail), _quantile(reps, 1 - tail))


def mean_ci(
    daily: Sequence[float],
    *,
    block: int,
    resamples: int,
    seed: int,
    level: float = 0.95,
    one_sided: bool = False,
    scheme: Scheme = "stationary",
) -> Interval:
    """CI of the mean of a per-day series (e.g. daily net P&L at stressed cost)."""
    return bootstrap_ci(
        daily,
        statistics.fmean,
        block=block,
        resamples=resamples,
        seed=seed,
        level=level,
        one_sided=one_sided,
        scheme=scheme,
    )


def _skill(days: Sequence[tuple[float, float]]) -> float:
    model = sum(m for m, _ in days)
    reference = sum(r for _, r in days)
    return 1 - model / reference if reference else 0.0


def brier_skill_ci(
    days: Sequence[tuple[float, float]],
    *,
    block: int,
    resamples: int,
    seed: int,
    level: float = 0.95,
    one_sided: bool = False,
    scheme: Scheme = "stationary",
) -> Interval:
    """CI of Brier skill 1 - sum(model Brier) / sum(reference Brier). Each unit is one
    day's (model Brier sum, reference Brier sum) over that day's predictions."""
    return bootstrap_ci(
        days,
        _skill,
        block=block,
        resamples=resamples,
        seed=seed,
        level=level,
        one_sided=one_sided,
        scheme=scheme,
    )


# --- sign test, Holm ---------------------------------------------------------------------


def sign_test(k: int, n: int) -> float:
    """One-sided exact p-value: P(X >= k) for X ~ Binomial(n, 1/2)."""
    if not 0 <= k <= n:
        raise ValueError("need 0 <= k <= n")
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (1 << n)


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values, in the input order; reject where <= alpha."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


# --- deflated Sharpe, minimum track record length -----------------------------------------


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] of `n_trials` skill-less trials whose SRs have variance `var_sr`."""
    if n_trials < 1 or var_sr < 0:
        raise ValueError("need n_trials >= 1 and var_sr >= 0")
    if n_trials == 1:
        return 0.0
    z = _PHI.inv_cdf
    factor = (1 - EULER_GAMMA) * z(1 - 1 / n_trials) + EULER_GAMMA * z(1 - 1 / (n_trials * math.e))
    return math.sqrt(var_sr) * factor


def _sr_variance_term(sr: float, skew: float, kurt: float) -> float:
    v = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if v <= 0:
        raise ValueError("skew and kurtosis give a non-positive SR variance")
    return v


def probabilistic_sharpe(sr: float, sr_star: float, *, t: float, skew: float, kurt: float) -> float:
    """P(true SR > sr_star) given an observed per-period `sr` over `t` periods."""
    if t <= 1:
        raise ValueError("need more than one period")
    v = _sr_variance_term(sr, skew, kurt)
    return _PHI.cdf((sr - sr_star) * math.sqrt(t - 1) / math.sqrt(v))


def deflated_sharpe(
    sr: float, *, t: float, skew: float, kurt: float, n_trials: int, var_sr: float
) -> float:
    """PSR against the expected maximum SR of `n_trials` skill-less trials."""
    sr0 = expected_max_sharpe(n_trials, var_sr)
    return probabilistic_sharpe(sr, sr0, t=t, skew=skew, kurt=kurt)


def min_track_record_length(
    sr: float, *, sr_star: float = 0.0, skew: float = 0.0, kurt: float = 3.0, alpha: float = 0.05
) -> float:
    """Periods needed for PSR(sr_star) to reach 1 - alpha; infinite if sr <= sr_star."""
    if sr <= sr_star:
        return math.inf
    z = _PHI.inv_cdf(1 - alpha)
    return 1 + _sr_variance_term(sr, skew, kurt) * (z / (sr - sr_star)) ** 2


# --- PBO by CSCV -------------------------------------------------------------------------


@dataclass(frozen=True)
class PBO:
    pbo: float
    logits: list[float]

    @property
    def combinations(self) -> int:
        return len(self.logits)


def _sharpe(total: float, squares: float, n: int) -> float:
    mean = total / n
    var = max(0.0, (squares - total * mean) / (n - 1)) if n > 1 else 0.0
    if var == 0:
        return 0.0 if mean == 0 else math.copysign(math.inf, mean)
    return mean / math.sqrt(var)


def pbo_cscv(matrix: Sequence[Sequence[float]], *, splits: int = 16) -> PBO:
    """Probability of backtest overfitting. `matrix[t][n]` is config n's out-of-sample
    result in period t. The rows are cut into `splits` contiguous blocks; every way of
    picking half of them as in-sample is one combination."""
    t_len = len(matrix)
    n_cfg = len(matrix[0]) if matrix else 0
    if splits < 2 or splits % 2 or splits > t_len:
        raise ValueError("splits must be even, at least 2, and at most the number of rows")
    if n_cfg < 2 or any(len(row) != n_cfg for row in matrix):
        raise ValueError("need a rectangular matrix with at least two configs")
    bounds = [round(i * t_len / splits) for i in range(splits + 1)]
    # Per block: row count, and per config the sum and sum of squares, so a combination
    # costs O(S x N) rather than O(T x N).
    blocks = list(pairwise(bounds))
    counts = [b1 - b0 for b0, b1 in blocks]
    sums = [[sum(matrix[t][c] for t in range(b0, b1)) for b0, b1 in blocks] for c in range(n_cfg)]
    sqs = [
        [sum(matrix[t][c] ** 2 for t in range(b0, b1)) for b0, b1 in blocks] for c in range(n_cfg)
    ]

    def scores(side: Sequence[int]) -> list[float]:
        n = sum(counts[b] for b in side)
        return [
            _sharpe(sum(sums[c][b] for b in side), sum(sqs[c][b] for b in side), n)
            for c in range(n_cfg)
        ]

    logits = []
    for chosen in combinations(range(splits), splits // 2):
        ins = scores(chosen)
        oos = scores(sorted(set(range(splits)) - set(chosen)))
        best = max(range(n_cfg), key=lambda c: ins[c])
        below = sum(s < oos[best] for s in oos)
        ties = sum(s == oos[best] for s in oos)  # includes the winner itself
        omega = (below + (ties + 1) / 2) / (n_cfg + 1)
        logits.append(math.log(omega / (1 - omega)))
    return PBO(sum(x <= 0 for x in logits) / len(logits), logits)


# --- ECE ---------------------------------------------------------------------------------


def _bins(ps: Sequence[float], ys: Sequence[bool], bins: int) -> list[list[tuple[float, bool]]]:
    if len(ps) != len(ys):
        raise ValueError("probabilities and outcomes differ in length")
    if bins < 1 or len(ps) < 2 * bins:
        raise ValueError("need at least two samples per bin")
    if any(not 0 <= p <= 1 for p in ps):
        raise ValueError("probabilities must lie in [0, 1]")
    # Sort by probability only: sorting the pairs would group tied forecasts by outcome.
    pairs = sorted(zip(ps, ys, strict=True), key=lambda pair: pair[0])
    n = len(pairs)
    return [pairs[round(i * n / bins) : round((i + 1) * n / bins)] for i in range(bins)]


def ece_binned(
    probabilities: Sequence[float], outcomes: Sequence[bool], *, bins: int = 15
) -> float:
    """Plug-in L1 ECE on equal-mass bins (biased upward when calibrated)."""
    n = len(probabilities)
    total = 0.0
    for b in _bins(probabilities, outcomes, bins):
        gap = statistics.fmean(p for p, _ in b) - statistics.fmean(float(y) for _, y in b)
        total += len(b) / n * abs(gap)
    return total


def ece_debiased(
    probabilities: Sequence[float], outcomes: Sequence[bool], *, bins: int = 15
) -> float:
    """Kumar et al. 2019 debiased L2 calibration error on equal-mass bins."""
    n = len(probabilities)
    total = 0.0
    for b in _bins(probabilities, outcomes, bins):
        p_bar = statistics.fmean(p for p, _ in b)
        y_bar = statistics.fmean(float(y) for _, y in b)
        total += len(b) / n * ((y_bar - p_bar) ** 2 - y_bar * (1 - y_bar) / (len(b) - 1))
    return math.sqrt(max(0.0, total))


def expected_binned_ece(n_eff: int, *, bins: int, p: float) -> float:
    """Closed-form noise floor of `ece_binned` for a perfectly calibrated model whose
    forecasts sit near `p`: each bin's gap is ~N(0, p(1-p)/(n_eff/bins)), and
    E|N(0, s^2)| = s * sqrt(2/pi)."""
    if n_eff < bins or not 0 < p < 1:
        raise ValueError("need n_eff >= bins and 0 < p < 1")
    return math.sqrt(2 / math.pi) * math.sqrt(p * (1 - p) * bins / n_eff)


@dataclass(frozen=True)
class NoiseFloor:
    mean: float
    p95: float


def ece_noise_floor(
    probabilities: Sequence[float],
    *,
    n_eff: int,
    bins: int = 15,
    sims: int = 200,
    seed: int,
    estimator: Callable[..., float] = ece_binned,
) -> NoiseFloor:
    """The ECE a perfectly calibrated model shows at `n_eff` samples: forecasts drawn
    from `probabilities`, outcomes drawn from the forecasts themselves."""
    rng = random.Random(seed)
    values = []
    for _ in range(sims):
        ps = rng.choices(probabilities, k=n_eff)
        values.append(estimator(ps, [rng.random() < p for p in ps], bins=bins))
    values.sort()
    return NoiseFloor(statistics.fmean(values), _quantile(values, 0.95))


# --- power -------------------------------------------------------------------------------


def simulated_power[S](
    draw: Callable[[random.Random], S], passes: Callable[[S], bool], *, sims: int, seed: int
) -> float:
    """Share of `sims` simulated samples (drawn under an assumed true effect) that pass."""
    if sims < 1:
        raise ValueError("sims must be positive")
    rng = random.Random(seed)
    return sum(passes(draw(rng)) for _ in range(sims)) / sims


def mean_lb_power(
    effect: float, sd: float, n: int, *, alpha: float = 0.05, sims: int = 2000, seed: int
) -> float:
    """Power of "one-sided (1 - alpha) lower bound of the mean > 0" on `n` independent
    units (e.g. days) with true mean `effect` and spread `sd` (normal approximation)."""
    if n < 2 or sd <= 0:
        raise ValueError("need n >= 2 and sd > 0")
    z = _PHI.inv_cdf(1 - alpha)

    def passes(xs: list[float]) -> bool:
        return statistics.fmean(xs) - z * statistics.stdev(xs) / math.sqrt(n) > 0

    return simulated_power(
        lambda rng: [rng.gauss(effect, sd) for _ in range(n)], passes, sims=sims, seed=seed
    )


# --- A/A noise floor, best-of-N ----------------------------------------------------------


class Comparison(StrEnum):
    BETTER = "BETTER"
    WORSE = "WORSE"
    UNCONFIRMED = "UNCONFIRMED"


def aa_noise_floor(seed_values: Sequence[float], *, z: float = 1.96) -> float:
    """Noise floor for a paired difference of two runs, from one config rerun with
    different seeds only: z * sqrt(2) * the seed-to-seed standard deviation."""
    if len(seed_values) < 2:
        raise ValueError("need at least two A/A runs")
    return z * math.sqrt(2) * statistics.stdev(seed_values)


def compare_to_floor(candidate: float, baseline: float, *, floor: float) -> Comparison:
    """Higher is better; a difference within the floor is unconfirmed."""
    diff = candidate - baseline
    if abs(diff) <= floor:
        return Comparison.UNCONFIRMED
    return Comparison.BETTER if diff > 0 else Comparison.WORSE


@dataclass(frozen=True)
class BestOfN:
    best: str
    value: float
    n_trials: int
    baseline: float
    noise_floor: float

    @property
    def lift(self) -> float:
        return self.value - self.baseline

    @property
    def confirmed(self) -> bool:
        """The lift over the fresh baseline exceeds the A/A noise floor."""
        return compare_to_floor(self.value, self.baseline, floor=self.noise_floor) is (
            Comparison.BETTER
        )


def best_of_n(
    scores: Mapping[str, float], *, baseline: float, n_trials: int, noise_floor: float
) -> BestOfN:
    """The best config (higher is better) against a baseline rerun on the same folds and
    seeds, reported with N. `n_trials` is the run log's counter and may exceed the
    scores shown (failed configs count), never fall below them."""
    if not scores:
        raise ValueError("no scores")
    if n_trials < len(scores):
        raise ValueError("n_trials cannot be smaller than the configs shown")
    best = max(scores, key=lambda k: scores[k])
    return BestOfN(best, scores[best], n_trials, baseline, noise_floor)
