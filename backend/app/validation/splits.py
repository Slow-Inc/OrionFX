"""Chronological walk-forward splits with an embargo and a sealed holdout (#66; VAL-02;
master plan §11.1: "cannot be optimized on final holdout").

The last `holdout` of [start, end) is set aside first; no fold reaches it. Folds roll
forward by the test length: train, an embargo gap (so labels that span the boundary
cannot leak), then test. Test windows are ordered and never overlap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Fold:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def walk_forward(
    start: datetime,
    end: datetime,
    *,
    train: timedelta,
    test: timedelta,
    embargo: timedelta,
    holdout: timedelta,
) -> tuple[list[Fold], tuple[datetime, datetime]]:
    zero = timedelta(0)
    if train <= zero or test <= zero or embargo < zero or holdout < zero:
        raise ValueError("train and test must be positive; embargo and holdout not negative")
    holdout_start = end - holdout
    folds: list[Fold] = []
    t = start
    while t + train + embargo + test <= holdout_start:
        folds.append(Fold(t, t + train, t + train + embargo, t + train + embargo + test))
        t += test
    if not folds:
        raise ValueError("no fold fits before the holdout")
    return folds, (holdout_start, end)
