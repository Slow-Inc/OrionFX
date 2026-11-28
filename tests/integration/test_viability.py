"""Protocol v1 §6 pre-run viability (#132, #128): FLAT share, dual-touch rate, no-label
rate and stressed cost/d per k, through the production label (`label_at`) and the
production ATR_M15 (the snapshot's technique). Hand-built bars where the answer is known:
- M15 bars are flat with a constant range R, so ATR_M15 = R exactly;
- M5 bars climb 1.0 a bar, so over H1 (12 bars) the bid reaches +12 from the entry close:
  UP when d = k*R <= 12, FLAT otherwise.
No real data."""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.bet import STRESSED_COST
from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.learning.bars import Horizon
from app.market_data.session import XAUUSD_NY_1700
from app.market_data.sources import Sealed, SourceRegistry
from app.market_data.store import CandleStore
from app.storage.db import transaction
from app.validation.viability import run_viability, to_markdown

D = Decimal
KS = (D("1.0"), D("1.25"), D("1.5"), D("2.0"))
H1 = Horizon(Timeframe.H1, 1)
# Wednesday 2023-03-01: before US DST, so the NY break is 22:00-23:00 UTC.
ERA = (datetime(2023, 3, 1, tzinfo=UTC), datetime(2023, 3, 1, 12, tzinfo=UTC))


def _store(conn: sqlite3.Connection, atr_range: Decimal) -> None:
    SourceRegistry(conn).register("v", "dukascopy-xauusd")
    store = CandleStore(conn, dataset_version="v")
    start, end = datetime(2023, 2, 27, tzinfo=UTC), datetime(2023, 3, 2, tzinfo=UTC)
    bars: list[CandleClosed] = []
    for tf in (Timeframe.M5, Timeframe.M15):
        size, t, i = TIMEFRAME_DURATION[tf], start, 0
        while t < end:
            if XAUUSD_NY_1700.expects(t, size):
                if tf is Timeframe.M5:  # climbs 1.0 a bar
                    o = D(2000) + i
                    lo, hi, c = o, o + 1, o + 1
                    i += 1
                else:  # flat, true range = atr_range
                    o = c = D(2000)
                    lo, hi = o - atr_range / 2, o + atr_range / 2
                bars.append(CandleClosed(
                    symbol="XAUUSD", timeframe=tf, open_time=t, close_time=t + size, open=o,
                    high=hi, low=lo, close=c, volume=D(1), spread=D("0.2"),
                    available_at=t + size))  # fmt: skip
            t += size
    with transaction(conn):
        for b in bars:
            store.save(b)


def _run(conn: sqlite3.Connection, **over: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {"dataset_version": "v", "era_start": ERA[0], "era_end": ERA[1],
                               "horizon": H1, "ks": KS}  # fmt: skip
    args.update(over)
    return run_viability(conn, **args)  # type: ignore[arg-type]


def test_a_k_that_reaches_the_barrier_and_is_cheap_enough_is_picked(
    conn: sqlite3.Connection,
) -> None:
    """R = 11: at k = 1, d = 11 <= 12, so every row is UP (FLAT 0%), and cost/d =
    1.07 / 11 = 0.0973 <= 0.10: k = 1.0 passes V1 and V2 and is the pick."""
    _store(conn, D(11))
    r = _run(conn)
    k1 = r.per_k[D("1.0")]
    overall = k1.groups["all"]
    assert overall.stats.rows == 44  # M15 closes 00:15..11:00, each with its H1 label inside
    assert overall.stats.up == 44 and overall.stats.flat == 0 and overall.stats.missing == 0
    fee = STRESSED_COST.fills + STRESSED_COST.commission
    assert fee == D("1.07") and overall.cost_d_median is not None
    assert abs(overall.cost_d_median - fee / 11) < D("1e-20")  # 0.0973 <= 0.10
    assert k1.v1 and k1.v2
    for k in KS[1:]:  # d = 13.75, 16.5, 22 > 12: all FLAT
        assert r.per_k[k].groups["all"].stats.flat == 44 and not r.per_k[k].v1
    assert r.pick == D("1.0") and r.verdict == "viable"


def test_no_k_passing_both_rules_is_non_viable(conn: sqlite3.Connection) -> None:
    """R = 10: k = 1 is all UP but cost/d = 0.107 > 0.10; wider k are all FLAT."""
    _store(conn, D(10))
    r = _run(conn)
    assert r.per_k[D("1.0")].v1 and not r.per_k[D("1.0")].v2
    assert r.pick is None and r.verdict == "non-viable"
    assert "non-viable" in to_markdown(r)


def test_years_and_sessions_are_reported(conn: sqlite3.Connection) -> None:
    _store(conn, D(11))
    groups = _run(conn).per_k[D("1.0")].groups
    assert groups["year:2023"].stats.rows == 44
    # London 08:00-17:00 Europe/London (UTC in March); before 08:00 UTC is Asia.
    assert groups["session:asia"].stats.rows == 31 and groups["session:london"].stats.rows == 13
    assert "session:ny" not in groups


def test_rows_without_atr_count_as_no_label(conn: sqlite3.Connection) -> None:
    """Before 71 M15 bars exist the snapshot's ATR has no value: no label, no cost."""
    _store(conn, D(11))
    early = _run(conn, era_start=datetime(2023, 2, 27, tzinfo=UTC),
                 era_end=datetime(2023, 2, 27, 12, tzinfo=UTC))  # fmt: skip
    stats = early.per_k[D("1.0")].groups["all"].stats
    assert stats.missing == stats.rows > 0
    assert early.per_k[D("1.0")].groups["all"].cost_d_median is None


def test_the_era_must_end_by_the_seal(conn: sqlite3.Connection) -> None:
    _store(conn, D(11))
    with pytest.raises(Sealed):
        _run(conn, era_end=datetime(2023, 11, 29, tzinfo=UTC))


def test_cost_d_uses_k_times_atr_and_the_pick_is_the_smallest_passing_k(
    conn: sqlite3.Connection,
) -> None:
    from types import SimpleNamespace

    from app.validation.viability import ViabilityResult

    _store(conn, D(11))
    median = _run(conn).per_k[D("2.0")].groups["all"].cost_d_median
    assert median is not None and abs(median - D("1.07") / 22) < D("1e-20")  # d = 2 x 11
    both, v1_only = SimpleNamespace(v1=True, v2=True), SimpleNamespace(v1=True, v2=False)
    per_k = {D("2.0"): both, D("1.25"): both, D("1.0"): v1_only}
    assert ViabilityResult("v", ERA, H1, per_k).pick == D("1.25")  # type: ignore[arg-type]
