"""Protocol v1 §6 pre-run viability (#132; data from #128). No model, no fit: price and
label statistics only, on development data, never past the seal.

For each k on the pre-declared grid, at the given horizon (H1 for B0), over every M15
decision point of the era:
- the tb.v2 label of the bar, through the production `label_at` (finest timeframe first),
  with d = k x ATR_M15 (the ATR is scaled by k / K, since `label_at` uses d = K x ATR);
- ATR_M15 as the snapshot computes it: the default technique behind `ATR_M15`, observed on
  the M15 bars known at the decision bar's `available_at`, on the source's schedule;
- stressed cost/d = (spread + 2 x slippage + commission) / d with `STRESSED_COST`, in real
  dollars (`cost_over_atr`).

Reported overall, per calendar year and per session (`asia`, `london`, `ny`: New York
08:00-17:00 local first, then London 08:00-17:00 local, else Asia; the city windows of
`app.prediction.transforms`). Shares follow `LabelStats`: FLAT/UP/DOWN over class-labelled
rows, dual touches over rows whose bars were known, no label over all rows.

Decision points: M15 bars opening in [era_start, era_end) whose close is inside an open
session (the bar closing at the daily break, the rollover, is not tradable) and whose
label window ends by `era_end` (purged by label end, so nothing past the era is read). A
row without an ATR (warm-up, a gap in the window) has no label and no cost.

Rule (pre-declared): V1 FLAT share <= 60%; V2 median stressed cost/d <= 0.10; the pick is
the smallest k passing both, else "non-viable".

Speed: the era's bars are read once per timeframe and served from memory to the
production `label_at` (`_Preloaded`), instead of one query per bar; no snapshot is built.
"""

import bisect
import math
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.bet import STRESSED_COST, cost_over_atr
from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.learning.bars import Horizon
from app.learning.labels import K, LabelStats, TripleBarrierLabel, label_at
from app.market_data.features import ATR_M15, DEFAULT_TECHNIQUES
from app.market_data.sources import SourceRegistry, require_unsealed
from app.market_data.store import CandleStore
from app.market_data.techniques.indicators import observe
from app.prediction.transforms import LONDON, NEW_YORK

__all__ = ["V1_MAX_FLAT", "V2_MAX_COST_D", "Group", "KResult", "ViabilityResult",
           "run_viability", "to_markdown"]  # fmt: skip

V1_MAX_FLAT = Decimal("0.60")
V2_MAX_COST_D = Decimal("0.10")
WARMUP = timedelta(days=7)
"""Bars read before the era: the ATR window (71 M15 bars) across a weekend."""
_ATR_TECHNIQUE = next(t for t in DEFAULT_TECHNIQUES if t.technique_id == ATR_M15.split(".")[1])
_ATR_OUTPUT = ATR_M15.split(".")[2]


class _Preloaded(CandleStore):
    """A dataset version's bars over one window, read once, served by `bar_at` from
    memory (the same answer as the store: the bar at `open_time` if available by
    `as_of`)."""

    def __init__(self, conn: sqlite3.Connection, version: str, symbol: str,
                 after: datetime, until: datetime) -> None:  # fmt: skip
        super().__init__(conn, dataset_version=version)
        self.loaded = {tf: self.bars(symbol, tf, as_of=until, after=after) for tf in Timeframe}
        self._by_open = {tf: {b.open_time: b for b in bars} for tf, bars in self.loaded.items()}

    def bar_at(self, symbol: str, timeframe: Timeframe, open_time: datetime, *,
               as_of: datetime) -> CandleClosed | None:  # fmt: skip
        b = self._by_open[timeframe].get(open_time)
        return b if b is not None and b.available_at <= as_of else None


@dataclass
class Group:
    labels: list[TripleBarrierLabel | None] = field(default_factory=list)
    cost_d: list[Decimal] = field(default_factory=list)

    @property
    def stats(self) -> LabelStats:
        return LabelStats.of(self.labels)

    @property
    def cost_d_median(self) -> Decimal | None:
        return statistics.median(self.cost_d) if self.cost_d else None

    def quantile(self, q: float) -> Decimal | None:
        """Nearest-rank quantile of cost/d."""
        if not self.cost_d:
            return None
        ordered = sorted(self.cost_d)
        return ordered[max(0, math.ceil(q * len(ordered)) - 1)]

    def summary(self) -> dict[str, Any]:
        s = self.stats
        labelled = s.up + s.down + s.flat

        def share(n: int, of: int) -> str | None:
            return None if of == 0 else str(Decimal(n) / of)

        return {
            "rows": s.rows, "missing": s.missing, "ambiguous": s.ambiguous,
            "up": s.up, "down": s.down, "flat": s.flat,
            "flat_share": share(s.flat, labelled), "up_share": share(s.up, labelled),
            "down_share": share(s.down, labelled),
            "dual_touch_rate": None if s.dual_touch_rate is None else str(s.dual_touch_rate),
            "no_label_rate": share(s.missing, s.rows),
            "cost_d_median": _str(self.cost_d_median), "cost_d_p75": _str(self.quantile(0.75)),
            "cost_d_p90": _str(self.quantile(0.90)),
        }  # fmt: skip


@dataclass
class KResult:
    k: Decimal
    groups: dict[str, Group]

    @property
    def v1(self) -> bool:
        flat = self.groups["all"].stats.flat_share
        return flat is not None and flat <= V1_MAX_FLAT

    @property
    def v2(self) -> bool:
        median = self.groups["all"].cost_d_median
        return median is not None and median <= V2_MAX_COST_D


@dataclass
class ViabilityResult:
    dataset_version: str
    era: tuple[datetime, datetime]
    horizon: Horizon
    per_k: dict[Decimal, KResult]

    @property
    def pick(self) -> Decimal | None:
        return next((k for k in sorted(self.per_k) if self.per_k[k].v1 and self.per_k[k].v2),
                    None)  # fmt: skip

    @property
    def verdict(self) -> str:
        return "viable" if self.pick is not None else "non-viable"

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": "m04 protocol v1 section 6", "dataset_version": self.dataset_version,
            "era": [self.era[0].isoformat(), self.era[1].isoformat()],
            "horizon": f"{self.horizon.timeframe.value}x{self.horizon.bars}",
            "rule": {"v1_max_flat": str(V1_MAX_FLAT), "v2_max_cost_d": str(V2_MAX_COST_D),
                     "stressed_cost": str(STRESSED_COST.fills + STRESSED_COST.commission)},
            "per_k": {str(k): {"v1": r.v1, "v2": r.v2,
                               "groups": {g: grp.summary() for g, grp in sorted(r.groups.items())}}
                      for k, r in sorted(self.per_k.items())},
            "pick": _str(self.pick), "verdict": self.verdict,
        }  # fmt: skip


def _str(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def _session_of(t: datetime) -> str:
    for name, (tz, start, end) in (("ny", NEW_YORK), ("london", LONDON)):
        local = t.astimezone(ZoneInfo(tz)).time()
        if start <= local < end:
            return name
    return "asia"


def run_viability(
    conn: sqlite3.Connection,
    *,
    dataset_version: str,
    era_start: datetime,
    era_end: datetime,
    horizon: Horizon,
    ks: Sequence[Decimal],
    symbol: str = "XAUUSD",
) -> ViabilityResult:
    registry = SourceRegistry(conn)
    require_unsealed(registry.source_of(dataset_version), era_end, start=era_start)
    session = registry.resolve_schedule(dataset_version, None)
    store = _Preloaded(conn, dataset_version, symbol, era_start - WARMUP, era_end)
    m15 = store.loaded[Timeframe.M15]
    known = [b.available_at for b in m15]
    points: list[tuple[datetime, Decimal | None]] = []
    for bar in m15:
        as_of = bar.close_time
        if bar.open_time < era_start or as_of + horizon.length > era_end:
            continue
        if session is not None and not session.is_open(as_of):
            continue  # the bar closing at the break: not a tradable decision point
        i = bisect.bisect_right(known, bar.available_at)
        window = m15[max(0, i - _ATR_TECHNIQUE.lookback) : i]
        obs = observe(_ATR_TECHNIQUE, window, bar.available_at, session=session)
        points.append((as_of, obs.values.get(_ATR_OUTPUT)))
    per_k = {}
    for k in ks:
        groups: dict[str, Group] = defaultdict(Group)
        for as_of, atr in points:
            scaled = None if atr is None else atr * k / K  # label_at's d = K x ATR
            lab = label_at(store, symbol, as_of, scaled, horizon, now=era_end, session=session)
            cost = None if atr is None or atr <= 0 else cost_over_atr(STRESSED_COST, atr * k / K)
            for key in ("all", f"year:{as_of.year}", f"session:{_session_of(as_of)}"):
                groups[key].labels.append(lab)
                if cost is not None:
                    groups[key].cost_d.append(cost)
        per_k[k] = KResult(k, dict(groups))
    return ViabilityResult(dataset_version, (era_start, era_end), horizon, per_k)


def to_markdown(r: ViabilityResult) -> str:
    def pct(x: str | None) -> str:
        return "n/a" if x is None else f"{float(x) * 100:.1f}%"

    def num(x: str | None) -> str:
        return "n/a" if x is None else f"{float(x):.4f}"

    lines = [f"# Viability (protocol v1 section 6): {r.dataset_version}",
             f"Era {r.era[0]:%Y-%m-%d} -> {r.era[1]:%Y-%m-%d}, horizon"
             f" {r.horizon.timeframe.value}x{r.horizon.bars}. V1: FLAT <= {V1_MAX_FLAT * 100:.0f}%;"
             f" V2: median stressed cost/d <= {V2_MAX_COST_D}.", "",
             "| k | group | rows | no label | dual touch | UP | DOWN | FLAT | cost/d median"
             " | p75 | p90 | V1 | V2 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for k, res in sorted(r.per_k.items()):
        for name, g in sorted(res.groups.items(), key=lambda kv: (kv[0] != "all", kv[0])):
            s = g.summary()
            v = (
                (("pass" if res.v1 else "fail"), ("pass" if res.v2 else "fail"))
                if name == "all"
                else ("", "")
            )
            lines.append(
                f"| {k} | {name} | {s['rows']} | {pct(s['no_label_rate'])} |"
                f" {pct(s['dual_touch_rate'])} | {pct(s['up_share'])} | {pct(s['down_share'])} |"
                f" {pct(s['flat_share'])} | {num(s['cost_d_median'])} | {num(s['cost_d_p75'])} |"
                f" {num(s['cost_d_p90'])} | {v[0]} | {v[1]} |"
            )  # fmt: skip
    lines += ["", f"**Verdict: {r.verdict}**"
              + ("" if r.pick is None else f", pick k = {r.pick}")]  # fmt: skip
    return "\n".join(lines) + "\n"
