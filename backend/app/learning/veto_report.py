"""How the M05 news veto is doing (#116, PRD #114).

Every entry refused with NEWS_VETO is an M07 experience carrying the side it refused
(the chart side) and, once known, the market's move over its horizon. Per veto:
- saved: the market moved against the refused side (the trade would have lost);
- missed: it moved with the refused side (the trade would have gained);
- flat: no move; unknown: no label known at `as_of` yet.
`mean_move_for_refused_side` averages the move signed in the refused side's favour over
labelled vetoes (positive means the veto mostly cost gains); None without any.
Read-only and point-in-time: only vetoes made by `as_of` and labels known at `as_of`
count. Moves are raw price units before costs, so a small "missed" move may not have
been a profitable trade. Vetoes on consecutive bars of one setup have overlapping
horizons, so one market move can be counted several times, and vetoes of different
horizons are averaged together: read the counts as a guide, not independent trials.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.enums import Direction, WaitReason
from app.learning.experience import ExperienceLog, LabelKind

__all__ = ["VetoReport", "veto_report"]


@dataclass(frozen=True, slots=True)
class VetoReport:
    vetoes: int
    saved: int
    missed: int
    flat: int
    unknown: int
    mean_move_for_refused_side: Decimal | None


def veto_report(
    log: ExperienceLog,
    *,
    start: datetime,
    end: datetime,
    as_of: datetime,
    reason: WaitReason = WaitReason.NEWS_VETO,
) -> VetoReport:
    """`reason` decisions (entry vetoes by default; NEWS_VETO_ADD for ADD vetoes, #121,
    never mixed in) made in `[start, end)` and by `as_of`, judged on labels known at
    `as_of`."""
    labels = log.labels(as_of=as_of)
    saved = missed = flat = unknown = 0
    favour: list[Decimal] = []
    vetoes = [
        e for e in log.experiences()
        if start <= e.as_of < end and e.as_of <= as_of
        and reason.value in e.reason_codes
        and e.direction in (Direction.UP, Direction.DOWN)
    ]  # fmt: skip
    for e in vetoes:
        label = labels.get(e.decision_id)
        if label is None or label.kind is not LabelKind.MARKET_MOVE:
            unknown += 1
            continue
        signed = label.value if e.direction is Direction.UP else -label.value
        favour.append(signed)
        if signed > 0:
            missed += 1
        elif signed < 0:
            saved += 1
        else:
            flat += 1
    mean = sum(favour, Decimal(0)) / len(favour) if favour else None
    return VetoReport(len(vetoes), saved, missed, flat, unknown, mean)
