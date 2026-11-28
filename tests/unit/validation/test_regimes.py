"""Regime buckets for the system backtest (#131; protocol v1 draft §7): trailing-only tags,
`unknown` on short history, and the coverage floor."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tests.support.backtest import line

from app.market_data.session import XAUUSD_NY_1700
from app.validation.regimes import (
    TRAILING_DAYS,
    TREND_DAYS,
    Coverage,
    CoverageFloor,
    DailyBar,
    era,
    protocol_v1_draft,
    session,
    trend,
    volatility,
)
from app.validation.system_backtest import daily_bars, trading_day

D = Decimal
T = datetime(2026, 1, 5, tzinfo=UTC)


def _days(closes: list[Decimal], vols: list[Decimal] | None = None) -> list[DailyBar]:
    vols = vols or [D(1)] * len(closes)
    return [DailyBar(date(2020, 1, 1) + timedelta(days=i), c, v, T)
            for i, (c, v) in enumerate(zip(closes, vols, strict=True))]  # fmt: skip


def test_sessions_follow_new_york_time_through_daylight_saving() -> None:
    # 13:00 UTC is 08:00 New York in winter (EST) and 09:00 in summer (EDT)
    assert session(datetime(2026, 1, 5, 13, tzinfo=UTC)) == "new-york"
    assert session(datetime(2026, 1, 5, 12, 59, tzinfo=UTC)) == "london"
    assert session(datetime(2026, 7, 6, 21, 30, tzinfo=UTC)) == "rollover"  # 17:30 EDT
    assert session(datetime(2026, 7, 6, 22, tzinfo=UTC)) == "asia"  # 18:00 EDT


def test_eras_use_the_protocol_boundaries() -> None:
    years = [2007, 2008, 2015, 2016, 2021, 2022, 2026]
    eras = ["pre-2008", "2008-2015", "2008-2015", "2016-2021", "2016-2021", "2022-end"]
    assert [era(datetime(y, 6, 1, tzinfo=UTC)) for y in years] == [*eras, "2022-end"]


def test_volatility_ranks_the_latest_day_against_the_trailing_252() -> None:
    base = [D(i % 50 + 1) for i in range(TRAILING_DAYS)]
    closes = [D(100)] * (TRAILING_DAYS + 1)
    assert volatility(_days(closes, [*base, D(1000)])) == "high"
    assert volatility(_days(closes, [*base, D(0)])) == "low"
    assert volatility(_days(closes, [*base, D(25)])) == "mid"
    assert volatility(_days(closes[1:], base)) == "unknown"  # 252 days: no trailing sample


def test_trend_is_the_z_score_of_the_latest_20_day_return() -> None:
    n = TRAILING_DAYS + TREND_DAYS
    wiggle = [D(100 + i % 3) for i in range(n)]  # 20-day returns vary a little
    assert trend(_days([*wiggle, wiggle[-TREND_DAYS] * D("1.5")])) == "trend-up"
    assert trend(_days([*wiggle, wiggle[-TREND_DAYS] * D("0.5")])) == "trend-down"
    assert trend(_days([*wiggle, wiggle[-TREND_DAYS]])) == "range"
    assert trend(_days(wiggle)) == "unknown"
    assert protocol_v1_draft(T, [])["trend"] == "unknown"


def test_a_trading_day_ends_at_the_session_close_and_is_known_only_then() -> None:
    # 21:45 UTC on Monday 5 Jan is 16:45 New York: still Monday's trading day; 23:00 UTC
    # (18:00 New York, the reopen) is Tuesday's.
    assert trading_day(datetime(2026, 1, 5, 21, 45, tzinfo=UTC), XAUUSD_NY_1700) == date(2026, 1, 5)
    assert trading_day(datetime(2026, 1, 5, 23, tzinfo=UTC), XAUUSD_NY_1700) == date(2026, 1, 6)
    bars = line(8, step=D(1))  # two hours of Monday 5 Jan, UTC days without a session
    (day,) = daily_bars(bars, None)
    assert day.close == bars[-1].close and day.known_at == datetime(2026, 1, 6, tzinfo=UTC)


def test_a_bucket_is_covered_only_with_enough_trades_and_days() -> None:
    floor = CoverageFloor()
    assert (floor.min_trades, floor.min_days) == (300, 60)
    assert floor.coverage(300, 60) is Coverage.COVERED
    assert floor.coverage(299, 500) is Coverage.NOT_COVERED
    assert floor.coverage(5000, 59) is Coverage.NOT_COVERED
