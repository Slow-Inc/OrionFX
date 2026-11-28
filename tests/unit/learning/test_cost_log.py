"""Measured spread per era (#128): information beside STRESSED_COST, never a bound. The
sealed windows of the protocol are never read."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.learning.cost_log import era_costs
from app.market_data.port import M1Bar

D = Decimal
M15 = timedelta(minutes=15)


def _m1(t: datetime, spread: str | None) -> M1Bar:
    p = D(2000)
    return M1Bar(t, p, p, p, p, D(1), None if spread is None else D(spread))


def _m15(start: datetime, n: int, rng: str) -> list[CandleClosed]:
    out, p = [], D(2000)
    for k in range(n):
        t = start + k * M15
        out.append(CandleClosed(symbol="XAUUSD", timeframe=Timeframe.M15, open_time=t,
                                close_time=t + M15, open=p, high=p + D(rng), low=p, close=p,
                                volume=D(1), available_at=t + M15))  # fmt: skip
    return out


def test_median_spread_and_spread_over_atr_per_year_before_the_seal() -> None:
    y1 = datetime(2021, 6, 1, tzinfo=UTC)
    y2 = datetime(2022, 6, 1, tzinfo=UTC)
    m15 = _m15(y1 - 20 * M15, 20, "2") + _m15(y2 - 20 * M15, 20, "2")  # ATR 2
    m1 = [_m1(y1, "0.2"), _m1(y1 + M15, "0.4"), _m1(y1 + 2 * M15, "0.3"), _m1(y1, None),
          _m1(y2, "0.4"),
          _m1(datetime(2025, 6, 1, tzinfo=UTC), "9")]  # sealed: never read  # fmt: skip
    (a, b) = era_costs(m1, m15, sealed_from=datetime(2025, 5, 28, tzinfo=UTC))
    assert (a.era, a.minutes, a.median_spread) == (2021, 3, D("0.3"))
    assert a.median_spread_over_atr == D("0.15")
    assert (b.era, b.minutes, b.median_spread, b.median_spread_over_atr) == (2022, 1, D("0.4"),
                                                                             D("0.2"))  # fmt: skip


def test_minutes_before_any_atr_have_a_spread_but_no_ratio() -> None:
    t = datetime(2008, 1, 2, tzinfo=UTC)
    (row,) = era_costs([_m1(t, "0.5")], [], sealed_from=datetime(2025, 5, 28, tzinfo=UTC))
    assert row.median_spread == D("0.5") and row.median_spread_over_atr is None
