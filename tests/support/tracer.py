"""Fixture and trainer for the training-loop tracer (#127). A support module, not a test
module.

Market: seeded synthetic XAUUSD M5 bars on the `XAUUSD_NY_1700` session (daily break and
weekend), with M15 and H1 resampled from them, as the historical import (#128) will do.
Prices trend in legs of 18-36 hours, so the chart has a direction that a trivial model can
learn and that `trend_following` (H1 ADX >= 25) can see. It is fixture data, never market
data, and proves nothing about edge.

Trainer: a two-feature logistic regression fitted by gradient descent on the train split,
then Platt-calibrated on the validation split, which comes later in time. Floats are used
here, offline; the weights are written as Decimal strings for the `logistic_json` adapter.
#130 replaces this with the real trainer behind the same adapter seam.
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
from app.decision.strategies import ADX_H1, ATR_M15, DIR_H1, DIR_M15, STRUCT_M15
from app.market_data.features import DEFAULT_TECHNIQUES
from app.market_data.session import SessionSchedule
from app.prediction.card import BASELINE_BRIER, BRIER
from app.validation.metrics import brier

D = Decimal
CENT = D("0.01")
M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1
LATENCY = timedelta(seconds=1)
SPREAD = D("0.20")
LEG_MIN, LEG_MAX = 216, 432
"""Trend legs last 18-36 hours of M5 bars."""

MODEL_FEATURES = [DIR_H1, DIR_M15]
CHAIN_KEYS = (DIR_H1, DIR_M15, STRUCT_M15, ADX_H1, ATR_M15)
"""What the chain reads: the model, `trend_following` and M05's EV (ATR_M15)."""

CHAIN_CATALOG = {"CAT-T03-07", "CAT-T03-04", "CAT-T01-07", "CAT-T01-06"}
TECHNIQUES = tuple(t for t in DEFAULT_TECHNIQUES if t.catalog_id in CHAIN_CATALOG)
"""The default library cut to the techniques behind `CHAIN_KEYS` (direction, structure,
ADX, ATR): the same ids, keys and values, at a fraction of the cost per bar."""
ADAPTER = "app.prediction.adapters.logistic_json"
ADAPTER_HASH = sha256_ref((Path(adapters.__file__).parent / "logistic_json.py").read_bytes())


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


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def _fit(xs: list[list[float]], ys: list[int], *, steps: int = 300, lr: float = 0.5
         ) -> tuple[list[float], float]:  # fmt: skip
    """Logistic regression by full-batch gradient descent: (coefficients, intercept)."""
    w, b, n = [0.0] * len(xs[0]), 0.0, len(xs)
    for _ in range(steps):
        gw, gb = [0.0] * len(w), 0.0
        for x, y in zip(xs, ys, strict=True):
            e = _sigmoid(b + sum(wi * xi for wi, xi in zip(w, x, strict=True))) - y
            gb += e
            gw = [g + e * xi for g, xi in zip(gw, x, strict=True)]
        b -= lr * gb / n
        w = [wi - lr * g / n for wi, g in zip(w, gw, strict=True)]
    return w, b


def _round(x: float) -> str:
    return f"{x:.6f}"


def _xy(rows: list[dict[str, Any]], label: str) -> tuple[list[list[float]], list[int]]:
    """Rows with every model feature and a non-zero move: (features, 1 if it rose)."""
    xs, ys = [], []
    for r in rows:
        values = [r["features"].get(k) for k in MODEL_FEATURES]
        move = None if r["labels"][label] is None else D(r["labels"][label]["move"])
        if None in values or not move:
            continue
        xs.append([float(v) for v in values])
        ys.append(int(move > 0))
    return xs, ys


def _brier(ps: list[float], ys: list[int]) -> Decimal:
    value = brier([D(_round(p)) for p in ps], [bool(y) for y in ys]).value
    assert value is not None
    return value


def train(rows: list[dict[str, Any]], label: str) -> tuple[bytes, dict[str, Any]]:
    """Fit on the `train` rows, calibrate on the later `validation` rows. Returns the
    weights file and what the card states: validation Brier against the train class
    prior, and the training cutoff (when the latest label read became known)."""
    by_split = {s: [r for r in rows if r["split"] == s] for s in ("train", "validation")}
    xs, ys = _xy(by_split["train"], label)
    coef, intercept = _fit(xs, ys)
    coef_s, intercept_s = [_round(c) for c in coef], _round(intercept)
    cx, cy = _xy(by_split["validation"], label)
    zs = [float(intercept_s) + sum(float(c) * v for c, v in zip(coef_s, x, strict=True))
          for x in cx]  # fmt: skip
    (a,), b = _fit([[z] for z in zs], cy)
    a_s, b_s = _round(a), _round(b)
    ps = [_sigmoid(float(a_s) * z + float(b_s)) for z in zs]
    prior = sum(ys) / len(ys)
    weights = json.dumps({"features": MODEL_FEATURES, "coef": coef_s, "intercept": intercept_s,
                          "platt": {"a": a_s, "b": b_s}}, sort_keys=True).encode()  # fmt: skip
    used = [r for s in by_split.values() for r in s if r["labels"][label] is not None]
    return weights, {
        "validation": {BRIER: _brier(ps, cy), BASELINE_BRIER: _brier([prior] * len(cy), cy)},
        "training_cutoff": max(datetime.fromisoformat(r["labels"][label]["available_at"])
                               for r in used),
    }  # fmt: skip
