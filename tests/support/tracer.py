"""Fixture and trainer for the training-loop tracer (#127). A support module, not a test
module.

Market: seeded synthetic XAUUSD M5 bars on the `XAUUSD_NY_1700` session (daily break and
weekend), with M15 and H1 resampled from them, as the historical import (#128) will do.
Prices trend in legs of 18-36 hours, so the chart has a direction that a trivial model can
learn and that `trend_following` (H1 ADX >= 25) can see. It is fixture data, never market
data, and proves nothing about edge.

Trainer: a three-class softmax regression on two M04 transforms, fitted by gradient
descent on the train split to the triple-barrier label (#129, `m07.labels.tb.v2`: UP,
DOWN or FLAT; AMBIGUOUS rows are not exported), then temperature-scaled on the
validation split, which comes later in time. Floats are used
here, offline; the weights are written as Decimal strings for the `barrier_softmax`
adapter. #130 replaces this with the real trainer behind the same adapter seam.
"""

import json
import math
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import app.prediction.adapters as adapters
from app.core.contracts import CandleClosed, sha256_ref
from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.decision.strategies import ADX_H1, ATR_M15, DIR_H1, STRUCT_M15
from app.market_data.features import DEFAULT_TECHNIQUES
from app.market_data.session import SessionSchedule
from app.prediction.card import BASELINE_BRIER, BRIER

D = Decimal
CENT = D("0.01")
M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1
LATENCY = timedelta(seconds=1)
SPREAD = D("0.20")
LEG_MIN, LEG_MAX = 216, 432
"""Trend legs last 18-36 hours of M5 bars."""

MODEL_FEATURES = ["H1.close_sma_atr", "M15.close_sma_atr"]
"""M04 transforms (`m04.transforms.v1`): (close - SMA20) / ATR14 per timeframe."""
CHAIN_KEYS = (DIR_H1, STRUCT_M15, ADX_H1, ATR_M15, "H1.sma_20.sma", "M15.sma_20.sma")
"""What the chain reads: `trend_following`, M05's EV (ATR_M15) and the model's inputs."""
CHAIN_CATALOG = {"CAT-T01-01", "CAT-T01-06", "CAT-T01-07", "CAT-T03-04", "CAT-T03-07"}
TECHNIQUES = tuple(t for t in DEFAULT_TECHNIQUES if t.catalog_id in CHAIN_CATALOG)
"""The default library cut to the techniques behind `CHAIN_KEYS` (SMA, ATR, ADX,
structure, direction): the same ids, keys and values, at a fraction of the cost per bar."""
ADAPTER = "app.prediction.adapters.barrier_softmax"
ADAPTER_HASH = sha256_ref((Path(adapters.__file__).parent / "barrier_softmax.py").read_bytes())
CLASSES = ("UP", "DOWN", "FLAT")
"""The adapter's row order."""


def _bar(tf: Timeframe, start: datetime, o: Decimal, h: Decimal, low: Decimal, c: Decimal,
         volume: Decimal) -> CandleClosed:  # fmt: skip
    close_time = start + TIMEFRAME_DURATION[tf]
    return CandleClosed(symbol="XAUUSD", timeframe=tf, open_time=start, close_time=close_time,
                        open=o, high=h, low=low, close=c, volume=volume, spread=SPREAD,
                        available_at=close_time + LATENCY)  # fmt: skip


def m5_bars(
    session: SessionSchedule, *, start: datetime, end: datetime, seed: int = 127
) -> list[CandleClosed]:
    """One M5 bar per open slot in [start, end): alternating trend legs (a drift of 0.25
    per bar) under uniform noise."""
    rng = random.Random(seed)
    out: list[CandleClosed] = []
    price, t, drift, leg_left = D(2000), start, D("0.25"), 0
    while t < end:
        if session.expects(t, TIMEFRAME_DURATION[M5]):
            if leg_left <= 0:
                drift, leg_left = -drift, rng.randint(LEG_MIN, LEG_MAX)
            leg_left -= 1
            close = (price + drift + D(str(round(rng.uniform(-0.6, 0.6), 2)))).quantize(CENT)
            wick = D(str(round(rng.uniform(0.1, 0.5), 2)))
            out.append(_bar(M5, t, price, max(price, close) + wick, min(price, close) - wick,
                            close, D(rng.randint(50, 150))))  # fmt: skip
            price = close
        t += TIMEFRAME_DURATION[M5]
    return out


def resample(bars: list[CandleClosed], tf: Timeframe) -> list[CandleClosed]:
    """Aggregate M5 bars into `tf` bars on UTC-aligned slots; a slot holds whatever open
    bars fall in it (the session closes on hour boundaries, so slots are whole)."""
    size = TIMEFRAME_DURATION[tf]
    groups: dict[datetime, list[CandleClosed]] = {}
    for b in bars:
        epoch = b.open_time.timestamp()
        slot = datetime.fromtimestamp(epoch - epoch % size.total_seconds(), tz=b.open_time.tzinfo)
        groups.setdefault(slot, []).append(b)
    out = []
    for slot, g in sorted(groups.items()):
        volume = sum((b.volume or D(0) for b in g), D(0))
        out.append(_bar(tf, slot, g[0].open, max(b.high for b in g), min(b.low for b in g),
                        g[-1].close, volume))  # fmt: skip
    return out


# --- the trivial trainer --------------------------------------------------------------------

Matrix = list[list[float]]


def _softmax(z: list[float]) -> list[float]:
    top = max(z)
    e = [math.exp(v - top) for v in z]
    return [v / sum(e) for v in e]


def _scores(b: list[float], w: Matrix, x: list[float]) -> list[float]:
    return [bk + sum(c * v for c, v in zip(wk, x, strict=True))
            for bk, wk in zip(b, w, strict=True)]  # fmt: skip


def _fit(xs: Matrix, ys: list[int], *, steps: int = 300, lr: float = 0.5
         ) -> tuple[list[float], Matrix]:  # fmt: skip
    """Softmax regression by full-batch gradient descent on standardised inputs, folded
    back to raw inputs: (intercepts, coefficients), one row per class."""
    k, n, m = len(CLASSES), len(xs), len(xs[0])
    mu = [sum(x[j] for x in xs) / n for j in range(m)]
    sd = [math.sqrt(sum((x[j] - mu[j]) ** 2 for x in xs) / n) or 1.0 for j in range(m)]
    zs = [[(x[j] - mu[j]) / sd[j] for j in range(m)] for x in xs]
    b, w = [0.0] * k, [[0.0] * m for _ in range(k)]
    for _ in range(steps):
        gb, gw = [0.0] * k, [[0.0] * m for _ in range(k)]
        for x, y in zip(zs, ys, strict=True):
            p = _softmax(_scores(b, w, x))
            for c in range(k):
                e = p[c] - (c == y)
                gb[c] += e
                gw[c] = [g + e * v for g, v in zip(gw[c], x, strict=True)]
        b = [bc - lr * g / n for bc, g in zip(b, gb, strict=True)]
        w = [[wj - lr * g / n for wj, g in zip(wc, gc, strict=True)]
             for wc, gc in zip(w, gw, strict=True)]  # fmt: skip
    coef = [[wc[j] / sd[j] for j in range(m)] for wc in w]
    return [b[c] - sum(coef[c][j] * mu[j] for j in range(m)) for c in range(k)], coef


def _round(x: float) -> str:
    return f"{x:.6f}"


def _xy(rows: list[dict[str, Any]], label: str) -> tuple[Matrix, list[int]]:
    """Rows with every model feature and a class label: (features, class index)."""
    xs, ys = [], []
    for r in rows:
        values = [r["features"].get(k) for k in MODEL_FEATURES]
        barrier = r["barrier_labels"][label]
        if None in values or barrier is None:
            continue
        xs.append([float(v) for v in values])
        ys.append(CLASSES.index(barrier["outcome"]))
    return xs, ys


def _nll(ps: Matrix, ys: list[int]) -> float:
    return -sum(math.log(max(p[y], 1e-12)) for p, y in zip(ps, ys, strict=True)) / len(ys)


def _brier(ps: Matrix, ys: list[int]) -> Decimal:
    """Multiclass Brier: the mean over rows of the squared error summed over classes."""
    return D(_round(sum(sum((pk - (k == y)) ** 2 for k, pk in enumerate(p))
                        for p, y in zip(ps, ys, strict=True)) / len(ys)))  # fmt: skip


def train(rows: list[dict[str, Any]], label: str) -> tuple[bytes, dict[str, Any]]:
    """Fit on fold 0's train rows, then the temperature on its later test rows.
    Returns the weights file and what the card states: the validation Brier against the
    train class priors, and the training cutoff (when the latest label read became
    known)."""
    by_split = {"train": [r for r in rows if 0 in r["folds"]["train"]],
                "validation": [r for r in rows if 0 in r["folds"]["test"]]}  # fmt: skip
    xs, ys = _xy(by_split["train"], label)
    b, w = _fit(xs, ys)
    b_s, w_s = [_round(v) for v in b], [[_round(v) for v in r] for r in w]
    vx, vy = _xy(by_split["validation"], label)
    z = [_scores([float(v) for v in b_s], [[float(v) for v in r] for r in w_s], x) for x in vx]
    grid = [0.25 + 0.05 * i for i in range(76)]
    t = _round(min(grid, key=lambda t: _nll([_softmax([v / t for v in zi]) for zi in z], vy)))
    prior = [ys.count(c) / len(ys) for c in range(len(CLASSES))]
    weights = json.dumps({"features": MODEL_FEATURES, "intercept": b_s, "coef": w_s,
                          "temperature": t}, sort_keys=True).encode()  # fmt: skip
    used = [r["barrier_labels"][label] for s in by_split.values() for r in s
            if r["barrier_labels"][label] is not None]  # fmt: skip
    return weights, {
        "validation": {BRIER: _brier([_softmax([v / float(t) for v in zi]) for zi in z], vy),
                       BASELINE_BRIER: _brier([prior] * len(vy), vy)},
        "training_cutoff": max(datetime.fromisoformat(b["available_at"]) for b in used),
    }  # fmt: skip
