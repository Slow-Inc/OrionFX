"""Indicator techniques CAT-T01-01..09 and tick volume CAT-T09-01 (#30; UST-02, UST-03).

Pure Decimal functions over closed bars of one symbol and timeframe, wrapped as
`TechniqueObservation`s. An observation never carries numbers when its inputs
cannot support them: too few bars (WARMUP), a GAP inside the window, or a last
bar too old to describe the market now (STALE) all give values of None and a
quality flag, so a consumer waits instead of acting on a false signal.

Formulas are the standard ones (Wilder smoothing for RSI/ATR/ADX, EMA seeded
with the SMA of its first window, population standard deviation for Bollinger
Bands). Default periods are [PROPOSED] (master plan: "exact periods TBD").
"""

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.contracts import CandleClosed, TechniqueObservation
from app.core.enums import Timeframe

D = Decimal
FRAME = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
}
BB_K = Decimal(2)
STALE_AFTER_BARS = 2  # [PROPOSED] a last bar older than two intervals is stale

Values = dict[str, Decimal]


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
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in itertools.pairwise(bars)
    ]


def rsi(closes: Sequence[Decimal], n: int) -> Decimal:
    deltas = [b - a for a, b in itertools.pairwise(closes)]
    gain = wilder([max(d, D(0)) for d in deltas], n)[-1]
    loss = wilder([max(-d, D(0)) for d in deltas], n)[-1]
    if loss == 0:
        return D(100) if gain > 0 else D(50)
    return D(100) - D(100) / (1 + gain / loss)


def dmi(bars: Sequence[CandleClosed], n: int) -> Values:
    plus_dm, minus_dm = [], []
    for p, b in itertools.pairwise(bars):
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
    version: str
    warmup: int
    unit: str
    compute: Callable[[Sequence[CandleClosed]], Values]
    flags: tuple[str, ...] = ()


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
    v = "1.0.0"
    techniques = [
        Technique(
            f"sma_{sma_n}",
            "CAT-T01-01",
            v,
            sma_n,
            "price",
            lambda b: {"sma": sma(_closes(b), sma_n)},
        ),
        Technique(
            f"ema_{ema_n}",
            "CAT-T01-02",
            v,
            ema_n,
            "price",
            lambda b: {"ema": ema_series(_closes(b), ema_n)[-1]},
        ),
        Technique(
            f"wma_{wma_n}",
            "CAT-T01-03",
            v,
            wma_n,
            "price",
            lambda b: {"wma": wma(_closes(b), wma_n)},
        ),
        Technique(
            f"rsi_{rsi_n}",
            "CAT-T01-04",
            v,
            rsi_n + 1,
            "index_0_100",
            lambda b: {"rsi": rsi(_closes(b), rsi_n)},
        ),
        Technique(
            f"macd_{macd_fast}_{macd_slow}_{macd_signal}",
            "CAT-T01-05",
            v,
            macd_slow + macd_signal - 1,
            "price",
            lambda b: macd(_closes(b), macd_fast, macd_slow, macd_signal),
        ),
        Technique(
            f"atr_{atr_n}",
            "CAT-T01-06",
            v,
            atr_n + 1,
            "price",
            lambda b: {"atr": wilder(true_ranges(b), atr_n)[-1]},
        ),
        Technique(
            f"adx_{adx_n}",
            "CAT-T01-07",
            v,
            2 * adx_n,
            "index_0_100",
            lambda b: {"adx": dmi(b, adx_n)["adx"]},
        ),
        Technique(
            f"dmi_{adx_n}",
            "CAT-T01-08",
            v,
            2 * adx_n,
            "index_0_100",
            lambda b: {k: x for k, x in dmi(b, adx_n).items() if k != "adx"},
        ),
        Technique(
            f"bb_{bb_n}_{bb_k}",
            "CAT-T01-09",
            v,
            bb_n,
            "price",
            lambda b: bollinger(_closes(b), bb_n, bb_k),
        ),
        Technique(
            "tick_volume",
            "CAT-T09-01",
            v,
            1,
            "broker_ticks",
            lambda b: {"tick_volume": b[-1].volume or D(0)},
            flags=("TICK_COUNT_NOT_TRADED_VOLUME",),
        ),
    ]
    return {t.technique_id: t for t in techniques}


def _ref(bar: CandleClosed) -> str:
    return f"{bar.symbol}:{bar.timeframe}:{bar.open_time.isoformat()}"


def observe(
    technique: Technique, bars: Sequence[CandleClosed], now: datetime
) -> TechniqueObservation:
    """One observation from closed bars (oldest first) of one symbol and timeframe."""
    if not bars:
        raise ValueError("observe needs at least one bar")
    last = bars[-1]
    if any(b.symbol != last.symbol or b.timeframe != last.timeframe for b in bars):
        raise ValueError("bars must share one symbol and timeframe")
    size = FRAME[last.timeframe]
    window = bars[-technique.warmup :]
    flags = list(technique.flags)
    if len(bars) < technique.warmup:
        flags.append("WARMUP")
    if any("GAP" in b.quality_flags for b in window):
        flags.append("GAP_IN_WINDOW")
    valid_until = last.close_time + size
    if now - last.close_time > STALE_AFTER_BARS * size or last.available_at >= valid_until:
        flags.append("STALE")  # too old now, or it arrived after its own validity
        valid_until = max(valid_until, last.available_at + timedelta(microseconds=1))
    usable = not ({"WARMUP", "GAP_IN_WINDOW", "STALE"} & set(flags))
    values: dict[str, Decimal | None]
    values = dict(technique.compute(bars)) if usable else {}
    return TechniqueObservation(
        technique_id=technique.technique_id,
        technique_version=technique.version,
        input_refs=[_ref(window[0]), _ref(last)],
        available_at=last.available_at,
        valid_until=valid_until,
        values=values,
        unit=technique.unit,
        uncertainty=None,
        quality_flags=flags,
        reason_codes=[] if usable else ["INPUT_NOT_USABLE"],
    )
