"""The system-tier backtest (#131, PRD #120 slice 4): a model's decisions replayed through the
real DecisionLoop, M06 and the fake broker, with M06's real exits.

Expected numbers are derived in each test from the bars and M06's rules (fills, the
Profit Target on Cycle Net, the position cap), never pasted from a run. Fixture bars are
not market data: none of this is a real-market result.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tests.support.backtest import (
    M15,
    T0,
    Always,
    constant_model,
    line,
    no_trade,
    store,
)

from app.core.bet import STRESSED_COST
from app.core.contracts import CandleClosed
from app.core.enums import ControlCommand, Direction, Mode, Side
from app.market_data.store import CandleStore
from app.prediction.engine import Refused, load_baseline
from app.risk.policy import DEFAULT_POLICY
from app.validation.evidence import EvidenceStore
from app.validation.regimes import Coverage, CoverageFloor, DailyBar
from app.validation.scenarios import Scenario, generate
from app.validation.system_backtest import (
    STRESSED,
    Account,
    BacktestSpec,
    LookAhead,
    PriceScale,
    SystemBacktestResult,
    run_system_backtest,
)

D = Decimal
CENT = D("0.01")
LOT = D("0.01")
TARGET = D(10)
ONE = Decimal(1)
A = 100
"""The first stepped bar: the feature library is warm by then."""
ACCOUNT = Account(balance=D(10000), initial_lot=LOT, profit_target_usd=TARGET,
                  max_drawdown_percent=D(20))  # fmt: skip
CAP = DEFAULT_POLICY.caps_for(ACCOUNT.risk_profile).max_positions
OZ = LOT * 100
"""Ounces per position: the fake broker's XAUUSD contract is 100 oz a lot."""


def _spec(bars: list[CandleClosed], steps: int, **over: object) -> BacktestSpec:
    spec = BacktestSpec(symbol="XAUUSD", timeframe=M15, context_timeframes=(),
                        start=bars[A].open_time, end=bars[A + steps - 1].close_time,
                        account=ACCOUNT, baseline_seed=None)  # fmt: skip
    return replace(spec, **over)  # type: ignore[arg-type]


def _run(conn: sqlite3.Connection, bars: list[CandleClosed], steps: int, *,
         favour: Direction = Direction.UP, candles: CandleStore | None = None,
         **over: object) -> SystemBacktestResult:  # fmt: skip
    candles = candles or store(conn, bars)
    model = constant_model(conn, favour=favour, cutoff=T0)
    return run_system_backtest(candles, model=model, strategies=[Always(favour)],
                               spec=_spec(bars, steps, **over), code_sha="test")  # fmt: skip


def _commission(factor: Decimal = ONE) -> Decimal:
    """Per deal: half the round-trip commission per oz, on the position's ounces, in cents."""
    return (STRESSED_COST.commission / 2 * OZ * factor).quantize(CENT)


def _fills(side: Side, close: Decimal, factor: Decimal = ONE) -> tuple[Decimal, Decimal]:
    """(entry, exit) at a bar close: the ask side pays the spread, every fill the slippage."""
    s, slip = STRESSED_COST.spread * factor, STRESSED_COST.slippage * factor
    if side is Side.BUY:
        return close + s + slip, close - slip
    return close - slip, close + s + slip


def _expected(bars: list[CandleClosed], side: Side, steps: int,
              factor: Decimal = ONE) -> list[tuple[int, int, Decimal]]:  # fmt: skip
    """(entry bar, exit bar, net) per position, by M06's rules: one entry per bar up to the
    position cap (the model and the chart keep agreeing), and at each later bar Cycle Net
    = floating at the bar's exit price - both commissions; the cycle closes at the first
    bar where it reaches the target, every position at that bar's exit fill."""
    sign, cm = (D(1) if side is Side.BUY else D(-1)), _commission(factor)
    held: list[int] = []
    for j in range(A, A + steps):
        mark = bars[j].close if side is Side.BUY else bars[j].close + STRESSED_COST.spread * factor
        net = sum((sign * (mark - _fills(side, bars[i].close, factor)[0]) * OZ - 2 * cm
                   for i in held), D(0))  # fmt: skip
        if held and net >= TARGET:
            out = _fills(side, bars[j].close, factor)[1]
            return [(i, j, sign * (out - _fills(side, bars[i].close, factor)[0]) * OZ - 2 * cm)
                    for i in held]  # fmt: skip
        if len(held) < CAP:
            held.append(j)
    raise AssertionError("the fixture never reaches the target")


LONG = (Direction.UP, Side.BUY, D(1))
SHORT = (Direction.DOWN, Side.SELL, D(-1))


@pytest.mark.parametrize(("favour", "side", "step"), [LONG, SHORT])
def test_a_known_model_on_a_known_line_earns_the_derived_pnl_under_the_real_exits(
    conn: sqlite3.Connection, favour: Direction, side: Side, step: Decimal
) -> None:
    bars = line(A + 12, step=step)
    expected = _expected(bars, side, steps=12)
    last = max(j for _, j, _ in expected)
    r = _run(conn, bars, last - A + 1, favour=favour)
    assert [(t.entry_time, t.exit_time, t.net_usd) for t in r.trades] == [
        (bars[i].available_at, bars[j].available_at, net) for i, j, net in expected
    ]
    assert {t.close_reason for t in r.trades} == {"TARGET_TRIGGERED"}
    assert {t.side for t in r.trades} == {side} and r.open_at_end == [] and r.refused == []
    assert r.summary.net_usd == sum(n for _, _, n in expected)
    assert sum(p for _, p in r.daily_pnl) == r.equity[-1][1] - ACCOUNT.balance == r.summary.net_usd


@pytest.mark.parametrize(("favour", "side", "step"), [LONG, SHORT])
def test_every_fill_pays_its_cost_on_entry_and_on_exit(
    conn: sqlite3.Connection, favour: Direction, side: Side, step: Decimal
) -> None:
    bars = line(A + 12, step=step)
    r = _run(conn, bars, 12, favour=favour)
    at = {b.available_at: b.close for b in bars}
    assert r.trades
    for t in r.trades:
        entry, _ = _fills(side, at[t.entry_time])
        _, out = _fills(side, at[t.exit_time])
        assert (t.entry_price, t.exit_price) == (entry, out)
        assert t.commission == 2 * _commission()
        gross = (out - entry) * OZ * (1 if side is Side.BUY else -1)
        assert t.net_usd == gross - t.commission


def test_a_ratio_adjusted_series_pays_the_fixed_dollar_cost_scaled_by_the_factor(
    conn: sqlite3.Connection,
) -> None:
    """FirstRate GC is ratio-adjusted (#128): a price at t is f(t) x the real one, so a
    fixed-dollar cost is charged as cost x f(t) in the series' units."""
    bars = line(A + 12, step=D(1), spread=None)
    half = D("0.5")
    cost = replace(STRESSED, scale=PriceScale("test-roll", ((T0, half),)))
    expected = _expected(bars, Side.BUY, 12, factor=half)
    r = _run(conn, bars, max(j for _, j, _ in expected) - A + 1, cost=cost)
    assert [(t.entry_time, t.net_usd) for t in r.trades] == [
        (bars[i].available_at, net) for i, _, net in expected
    ]
    at = {b.available_at: b.close for b in bars}
    assert all(t.entry_price == _fills(Side.BUY, at[t.entry_time], half)[0] for t in r.trades)
    assert all(t.commission == 2 * _commission(half) for t in r.trades)


def test_the_price_scale_steps_at_each_roll_and_refuses_a_time_before_it() -> None:
    later = T0 + timedelta(days=30)
    scale = PriceScale("rolls", ((T0, D("0.9")), (later, D(1))))
    assert (scale.at(T0), scale.at(later - timedelta(seconds=1)), scale.at(later)) == (
        D("0.9"),
        D("0.9"),
        D(1),
    )
    with pytest.raises(ValueError, match="no price scale factor"):
        scale.at(T0 - timedelta(seconds=1))
    bar = line(1, step=D(1), spread=D("0.30"))[0]
    stressed = replace(STRESSED, scale=scale)
    assert stressed.spread(bar) == max(D("0.30"), STRESSED_COST.spread * D("0.9"))
    assert STRESSED.spread(bar.model_copy(update={"spread": D("1.50")})) == D(
        "1.50"
    )  # a wider bar spread still counts


def test_an_emergency_stop_closes_the_basket_and_m06_refuses_entries_after_it(
    conn: sqlite3.Connection,
) -> None:
    bars = line(A + 8, step=D(1))
    stop_at = bars[A + 4].available_at
    far = replace(ACCOUNT, profit_target_usd=D(1000))  # the target never fires first
    r = _run(conn, bars, 8, account=far, controls=((stop_at, ControlCommand.EMERGENCY_STOP),))
    assert r.trades and {t.close_reason for t in r.trades} == {"EMERGENCY_STOP"}
    assert {t.exit_time for t in r.trades} == {stop_at} and r.open_at_end == []
    after = [x for x in r.refused if x.time > stop_at]
    assert after and all("CONTROL_STATE_NO_ENTRY" in x.reasons for x in after)


def test_a_drawdown_breach_locks_and_closes_with_the_risk_limit_reason(
    conn: sqlite3.Connection,
) -> None:
    bars = line(A + 10, step=D(-1))
    tiny = replace(ACCOUNT, balance=D(100), max_drawdown_percent=D(5))
    r = _run(conn, bars, 10, account=tiny)  # a long basket on a falling line
    assert r.trades and {t.close_reason for t in r.trades} == {"RISK_LIMIT"}
    assert r.max_drawdown_percent >= 5


def test_no_trade_bars_never_trigger_an_entry_or_an_exit(conn: sqlite3.Connection) -> None:
    """A NO_TRADE bar (#128) brings no quote: the last one goes stale, M05 decides nothing
    on it, and M06's exits run at an unchanged price, so neither fires on that bar."""
    bars = line(A + 14, step=D(1))
    flat = [A, A + 2, A + 5, A + 6]  # the first entry bar, an add bar, and two while held
    for i in flat:
        bars[i] = no_trade(bars[i], bars[i - 1])
    r = _run(conn, bars, 14)
    quiet = {bars[i].available_at for i in flat}
    assert r.trades
    assert not quiet & {t for x in r.trades for t in (x.entry_time, x.exit_time)}
    assert r.no_decision == {"DEGRADED: market quote stale": len(flat)}


def test_a_future_bar_read_fails_the_run(conn: sqlite3.Connection) -> None:
    class Leaky(CandleStore):
        """A broken store: it ignores `as_of` and returns bars not yet available."""

        def bars(self, symbol, timeframe, *, as_of, limit=None, after=None):  # type: ignore[no-untyped-def]
            far = as_of + timedelta(days=365)
            return super().bars(symbol, timeframe, as_of=far, limit=limit, after=after)

    bars = line(A + 8, step=D(1))
    store(conn, bars)
    with pytest.raises(LookAhead):
        _run(conn, bars, 4, candles=Leaky(conn, dataset_version="bt-v1"))


def test_bars_after_the_window_change_nothing(conn: sqlite3.Connection) -> None:
    bars = line(A + 8, step=D(1))
    plain = _run(conn, bars, 6)
    wild = [*bars, *line(20, step=D(50), start=bars[-1].close_time, base=bars[-1].close)]
    later = _run(conn, wild, 6, candles=store(conn, wild, "bt-v1-wild"))
    assert plain.body() | {"manifest": None} == later.body() | {"manifest": None}


def test_the_same_inputs_give_the_same_output_hash_baseline_included(
    conn: sqlite3.Connection,
) -> None:
    bars = generate(Scenario.STRONG_TREND, start=T0, bars=A + 40)
    candles = store(conn, bars)
    small = replace(ACCOUNT, profit_target_usd=D(2))  # many short cycles

    def run(seed: int) -> SystemBacktestResult:
        return _run(conn, bars, 40, candles=candles, baseline_seed=seed, account=small)

    first, again = run(7), run(7)
    assert first.output_hash == again.output_hash
    assert first.trades and first.baselines["random_entry"].trades  # both did trade
    other = run(8)
    assert other.baselines["random_entry"] != first.baselines["random_entry"]
    assert first.baselines["no_trade"].trades == 0


def test_thin_buckets_are_not_covered_and_never_listed_as_evidence(
    conn: sqlite3.Connection,
) -> None:
    bars = line(A + 12, step=D(1))
    thin = _run(conn, bars, 12)
    assert thin.regimes and {b.coverage for b in thin.regimes} == {Coverage.NOT_COVERED}
    assert {b.name for b in thin.regimes if b.dimension in ("volatility", "trend")} == {
        "unknown"
    }  # no 252-day history: never guessed
    assert thin.validation_run().regimes == []
    n = thin.summary.trades
    at_floor = _run(conn, bars, 12, floor=CoverageFloor(min_trades=n, min_days=1))
    assert {b.coverage for b in at_floor.regimes} == {Coverage.COVERED}
    below = _run(conn, bars, 12, floor=CoverageFloor(min_trades=n + 1, min_days=1))
    assert {b.coverage for b in below.regimes} == {Coverage.NOT_COVERED}


def test_the_run_is_stored_as_evidence_with_its_manifest(conn: sqlite3.Connection) -> None:
    bars = line(A + 12, step=D(1))
    r = _run(conn, bars, 12)
    m = r.manifest
    assert (m.code_sha, m.dataset_version, m.spec.cost.mode) == ("test", "bt-v1", "STRESSED")
    assert m.card_pin.startswith("card:") and m.weights_hash and m.protocol_hash is None
    evidence = EvidenceStore(conn)
    run = evidence.run(evidence.record_run(r.validation_run()))
    assert run is not None and run.config["output_hash"] == r.output_hash
    assert run.trades == r.summary.trades
    assert r.expectancy_ci(lambda days: (min(min(d) for d in days), max(max(d) for d in days))) == (
        min(t.net_usd for t in r.trades),
        max(t.net_usd for t in r.trades),
    )


def test_a_live_path_or_raw_model_is_refused(conn: sqlite3.Connection) -> None:
    bars = line(A + 4, step=D(1))
    candles = store(conn, bars)
    model = constant_model(conn, favour=Direction.UP, cutoff=T0)
    raw = model.predictor
    live = load_baseline(replace(raw, card=raw.card.model_copy(
        update={"artifact_hash": None})), mode=Mode.DEMO)  # fmt: skip
    for wrong in (live, raw):
        with pytest.raises(Refused, match="load_for_evaluation"):
            run_system_backtest(candles, model=wrong, strategies=[Always(Direction.UP)],
                                spec=_spec(bars, 2), code_sha="t")  # fmt: skip
    late = constant_model(conn, favour=Direction.DOWN, cutoff=bars[A].open_time)
    with pytest.raises(Refused, match="training cutoff"):
        run_system_backtest(candles, model=late, strategies=[Always(Direction.DOWN)],
                            spec=_spec(bars, 2), code_sha="t")  # fmt: skip


def test_scenarios_run_as_behaviour_tests(conn: sqlite3.Connection) -> None:
    """Behaviour, not edge: on the wide-spread scenario M05 prices the spread into EV and
    waits; on a strong trend the same model trades."""
    wide = generate(Scenario.WIDE_SPREAD, start=T0, bars=A + 10)
    r = _run(conn, wide, 10)
    assert r.trades == [] and r.refused == []
    assert r.decisions == {"WAIT:EV_NOT_POSITIVE": 10}
    trend = generate(Scenario.STRONG_TREND, start=T0, bars=A + 20)
    assert _run(conn, trend, 20, candles=store(conn, trend, "bt-trend")).trades


def test_m06_denies_a_spread_past_its_limit_and_the_next_bar_still_decides(
    conn: sqlite3.Connection,
) -> None:
    """#121: after a DENY, M06 has decided after its last positions snapshot. With the
    clock on bar availability the next bar's first M06 tick reports positions again, so
    the 30 s staleness rule never stalls the loop: every bar gets a decision."""
    bars = line(A + 6, step=D(5), spread=D("1.00"))  # ATR 6: EV clears a 1.00 spread
    r = _run(conn, bars, 6)
    assert len(r.refused) == 6 and all(x.reasons == ["SPREAD_TOO_WIDE"] for x in r.refused)
    assert r.trades == [] and r.no_decision == {}
    assert sum(r.decisions.values()) == r.steps == 6


def test_regime_tags_see_only_days_that_had_ended(conn: sqlite3.Connection) -> None:
    seen: list[tuple[datetime, list[datetime]]] = []

    def spy(t: datetime, days: Sequence[DailyBar]) -> dict[str, str]:
        seen.append((t, [d.known_at for d in days]))
        return {"probe": "x"}

    bars = line(A + 12, step=D(1))
    model = constant_model(conn, favour=Direction.UP, cutoff=T0)
    r = run_system_backtest(store(conn, bars), model=model, strategies=[Always(Direction.UP)],
                            spec=_spec(bars, 12), code_sha="t", regimes=spy)  # fmt: skip
    assert r.trades and seen and all(k <= t for t, known in seen for k in known)
    assert any(known for _, known in seen)  # the day before the window had ended
