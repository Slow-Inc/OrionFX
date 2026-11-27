"""Indicator techniques CAT-T01-01..09 and tick volume CAT-T09-01 (#30; UST-02, UST-03).

Pure Decimal functions over closed bars of one symbol and timeframe, wrapped as
`TechniqueObservation`s. Each technique reads exactly its last `lookback` bars,
so a value depends only on the bars named in `input_refs`, never on how much
history the caller passed (smoothed indicators use a fixed multiple of their
period [PROPOSED]). An observation never carries numbers its inputs cannot
support: too few bars (WARMUP), missing or non-contiguous bars (GAP_IN_WINDOW),
a missing input such as unknown volume (MISSING_INPUT) or an expired last bar
(STALE) give an empty `values` and a quality flag, so a consumer waits instead
of acting on a false signal. Asking with `now` before the last bar was
available is refused: that would be look-ahead.

Formulas are the standard ones (Wilder smoothing for RSI/ATR/ADX, EMA seeded
with the SMA of its first window, population standard deviation for Bollinger
Bands). Default periods are [PROPOSED] (master plan: "exact periods TBD").
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from app.core.contracts import CandleClosed, TechniqueObservation
from app.core.enums import QualityFlag
from app.core.time import TIMEFRAME_DURATION
from app.market_data.session import SessionSchedule, next_slot

D = Decimal
BB_K = Decimal(2)
RECURSIVE_LOOKBACK = 5  # [PROPOSED] smoothed indicators read 5x their period

Values = dict[str, Decimal]
OptValues = dict[str, Decimal | None]
"""For techniques where 'not present' (e.g. no level found) is a real answer."""


def adx_lookback(n: int) -> int:
    return (RECURSIVE_LOOKBACK + 1) * n + 1


# --- series math (pure) ----------------------------------------------------------------------


def sma(xs: Sequence[Decimal], n: int) -> Decimal:
    return sum(xs[-n:], D(0)) / n


def wma(xs: Sequence[Decimal], n: int) -> Decimal:
    window = xs[-n:]
    return sum((x * (i + 1) for i, x in enumerate(window)), D(0)) / D(n * (n + 1) // 2)


def ema_series(xs: Sequence[Decimal], n: int) -> list[Decimal]:
    """EMA values from index n-1 on, seeded with the SMA of the first n."""
    alpha = D(2) / D(n + 1)
    out = [sum(xs[:n], D(0)) / n]
    for x in xs[n:]:
        out.append(out[-1] + alpha * (x - out[-1]))
    return out


def wilder(xs: Sequence[Decimal], n: int) -> list[Decimal]:
    """Wilder smoothing: seed with the mean of the first n, then (prev*(n-1)+x)/n."""
    out = [sum(xs[:n], D(0)) / n]
    for x in xs[n:]:
        out.append((out[-1] * (n - 1) + x) / n)
    return out


def true_ranges(bars: Sequence[CandleClosed]) -> list[Decimal]:
    return [
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in pairwise(bars)
    ]


def rsi(closes: Sequence[Decimal], n: int) -> Decimal:
    deltas = [b - a for a, b in pairwise(closes)]
    gain = wilder([max(d, D(0)) for d in deltas], n)[-1]
    loss = wilder([max(-d, D(0)) for d in deltas], n)[-1]
    if loss == 0:
        return D(100) if gain > 0 else D(50)
    return D(100) - D(100) / (1 + gain / loss)


def dmi(bars: Sequence[CandleClosed], n: int) -> Values:
    plus_dm, minus_dm = [], []
    for p, b in pairwise(bars):
        up, down = b.high - p.high, p.low - b.low
        plus_dm.append(up if up > down and up > 0 else D(0))
        minus_dm.append(down if down > up and down > 0 else D(0))
    tr = wilder(true_ranges(bars), n)
    pdi = [D(100) * a / t if t else D(0) for a, t in zip(wilder(plus_dm, n), tr, strict=True)]
    mdi = [D(100) * a / t if t else D(0) for a, t in zip(wilder(minus_dm, n), tr, strict=True)]
    dx = [D(100) * abs(p - m) / (p + m) if p + m else D(0) for p, m in zip(pdi, mdi, strict=True)]
    return {"plus_di": pdi[-1], "minus_di": mdi[-1], "adx": wilder(dx, n)[-1]}


def bollinger(closes: Sequence[Decimal], n: int, k: Decimal) -> Values:
    mid = sma(closes, n)
    var = sum(((x - mid) ** 2 for x in closes[-n:]), D(0)) / n
    sd = var.sqrt()
    return {"middle": mid, "upper": mid + k * sd, "lower": mid - k * sd}


def macd(closes: Sequence[Decimal], fast: int, slow: int, signal: int) -> Values:
    fast_e = ema_series(closes, fast)[slow - fast :]
    slow_e = ema_series(closes, slow)
    line = [f - s for f, s in zip(fast_e, slow_e, strict=True)]
    sig = ema_series(line, signal)[-1]
    return {"macd": line[-1], "signal": sig, "histogram": line[-1] - sig}


# --- techniques --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Technique:
    technique_id: str
    catalog_id: str
    lookback: int
    """Exactly this many closed bars are read; fewer means WARMUP."""
    unit: str
    compute: Callable[[Sequence[CandleClosed]], Mapping[str, Decimal | None]]
    outputs: tuple[str, ...] = ()
    """The value keys it always reports: a feature schema stays the same shape."""
    flags: tuple[QualityFlag, ...] = ()
    needs_volume: bool = False
    version: str = "1.0.0"


def _closes(bars: Sequence[CandleClosed]) -> list[Decimal]:
    return [b.close for b in bars]


def make_library(
    *,
    sma_n: int = 20,
    ema_n: int = 20,
    wma_n: int = 20,
    rsi_n: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    atr_n: int = 14,
    adx_n: int = 14,
    bb_n: int = 20,
    bb_k: Decimal = BB_K,
) -> dict[str, Technique]:
    """The MVP indicator set; parameters are part of each technique's identity."""
    r = RECURSIVE_LOOKBACK
    techniques = [
        Technique(
            f"sma_{sma_n}",
            "CAT-T01-01",
            lookback=sma_n,
            unit="price",
            outputs=("sma",),
            compute=lambda b: {"sma": sma(_closes(b), sma_n)},
        ),
        Technique(
            f"ema_{ema_n}",
            "CAT-T01-02",
            lookback=r * ema_n,
            unit="price",
            outputs=("ema",),
            compute=lambda b: {"ema": ema_series(_closes(b), ema_n)[-1]},
        ),
        Technique(
            f"wma_{wma_n}",
            "CAT-T01-03",
            lookback=wma_n,
            unit="price",
            outputs=("wma",),
            compute=lambda b: {"wma": wma(_closes(b), wma_n)},
        ),
        Technique(
            f"rsi_{rsi_n}",
            "CAT-T01-04",
            lookback=r * rsi_n + 1,
            unit="index_0_100",
            outputs=("rsi",),
            compute=lambda b: {"rsi": rsi(_closes(b), rsi_n)},
        ),
        Technique(
            f"macd_{macd_fast}_{macd_slow}_{macd_signal}",
            "CAT-T01-05",
            lookback=r * macd_slow + macd_signal,
            unit="price",
            outputs=("macd", "signal", "histogram"),
            compute=lambda b: macd(_closes(b), macd_fast, macd_slow, macd_signal),
        ),
        Technique(
            f"atr_{atr_n}",
            "CAT-T01-06",
            lookback=r * atr_n + 1,
            unit="price",
            outputs=("atr",),
            compute=lambda b: {"atr": wilder(true_ranges(b), atr_n)[-1]},
        ),
        Technique(
            f"adx_{adx_n}",
            "CAT-T01-07",
            lookback=adx_lookback(adx_n),
            unit="index_0_100",
            outputs=("adx",),
            compute=lambda b: {"adx": dmi(b, adx_n)["adx"]},
        ),
        Technique(
            f"dmi_{adx_n}",
            "CAT-T01-08",
            lookback=adx_lookback(adx_n),
            unit="index_0_100",
            outputs=("plus_di", "minus_di"),
            compute=lambda b: {k: x for k, x in dmi(b, adx_n).items() if k != "adx"},
        ),
        Technique(
            f"bb_{bb_n}_{bb_k.normalize()}",
            "CAT-T01-09",
            lookback=bb_n,
            unit="price",
            outputs=("middle", "upper", "lower"),
            compute=lambda b: bollinger(_closes(b), bb_n, bb_k),
        ),
        Technique(
            "tick_volume",
            "CAT-T09-01",
            lookback=1,
            unit="broker_ticks",
            outputs=("tick_volume",),
            compute=lambda b: {"tick_volume": b[-1].volume or D(0)},
            flags=(QualityFlag.TICK_COUNT_NOT_TRADED_VOLUME,),
            needs_volume=True,
        ),
    ]
    return {t.technique_id: t for t in techniques}


def _ref(bar: CandleClosed) -> str:
    return f"{bar.symbol}:{bar.timeframe}:{bar.open_time.isoformat()}"


def observe(
    technique: Technique,
    bars: Sequence[CandleClosed],
    now: datetime,
    *,
    session: SessionSchedule | None = None,
) -> TechniqueObservation:
    """One observation from closed bars (oldest first) of one symbol and timeframe.

    With a `session`, bars are expected only while the market is open: a window that
    spans a scheduled closure is contiguous, and the last bar before one stays valid
    only until the market reopens (safety over availability: the reopen is when spreads
    are widest). Without one the market never closes (24/7)."""
    if not bars:
        raise ValueError("observe needs at least one bar")
    last = bars[-1]
    if any(b.symbol != last.symbol or b.timeframe != last.timeframe for b in bars):
        raise ValueError("bars must share one symbol and timeframe")
    if now < last.available_at:
        raise ValueError("the last bar was not available yet at `now` (look-ahead)")
    size = TIMEFRAME_DURATION[last.timeframe]
    window = bars[-technique.lookback :]
    blockers: list[QualityFlag] = []
    if len(window) < technique.lookback:
        blockers.append(QualityFlag.WARMUP)
    contiguous = all(b.open_time == next_slot(session, a.open_time, size)
                     for a, b in pairwise(window))  # fmt: skip
    if not contiguous or any(QualityFlag.GAP in b.quality_flags for b in window):
        blockers.append(QualityFlag.GAP_IN_WINDOW)  # no indicator spans missing data
    if technique.needs_volume and last.volume is None:
        blockers.append(QualityFlag.MISSING_INPUT)
    # The next bar replaces this one. Across a scheduled closure it is valid only until
    # the market reopens: after that, the next bar must close before it counts as fresh
    # (safety over availability; the reopen is when spreads are widest).
    reopen = next_slot(session, last.open_time, size)
    valid_until = reopen + size if reopen == last.close_time else reopen
    if now >= valid_until:
        blockers.append(QualityFlag.STALE)
        # TechniqueObservation needs valid_until > available_at even when stale.
        valid_until = max(valid_until, last.available_at + size)
    return TechniqueObservation(
        technique_id=technique.technique_id,
        technique_version=technique.version,
        input_refs=[_ref(window[0]), _ref(last)],
        available_at=last.available_at,
        valid_until=valid_until,
        values={} if blockers else dict(technique.compute(window)),
        unit=technique.unit,
        uncertainty=None,
        quality_flags=[*technique.flags, *blockers],
        reason_codes=["INPUT_NOT_USABLE"] if blockers else [],
    )
