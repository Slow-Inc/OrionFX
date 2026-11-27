"""Chronological walk-forward splits with an embargo and a sealed holdout (#66; VAL-02;
master plan §11.1: "cannot be optimized on final holdout").

The last `holdout` of [start, end) is set aside first, and an embargo separates it from
the last test window too, so no label spanning that boundary sees holdout prices. Folds roll
forward by the test length: train, an embargo gap (so labels that span the boundary
cannot leak), then test. Test windows are ordered and never overlap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Fold:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass(frozen=True)
class WalkForward:
    folds: list[Fold]
    holdout: Window
    """Sealed: for the final evaluation only, never for tuning (master plan §11.1)."""


def walk_forward(
    start: datetime,
    end: datetime,
    *,
    train: timedelta,
    test: timedelta,
    embargo: timedelta,
    holdout: timedelta,
) -> WalkForward:
    zero = timedelta(0)
    if start >= end:
        raise ValueError("start must be before end")
    if train <= zero or test <= zero or embargo < zero or holdout < zero:
        raise ValueError("train and test must be positive; embargo and holdout not negative")
    holdout_start = end - holdout
    folds: list[Fold] = []
    t = start
    while t + train + embargo + test + embargo <= holdout_start:
        folds.append(Fold(t, t + train, t + train + embargo, t + train + embargo + test))
        t += test
    if not folds:
        raise ValueError("no fold fits before the holdout")
    return WalkForward(folds, Window(holdout_start, end))
