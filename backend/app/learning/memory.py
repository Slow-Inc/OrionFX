"""Point-in-time similarity retrieval, semantic memory and RAG context (#60; LRN-01,
LRN-02, LRN-03; AT-15; CAT-A04-03).

- Episodic memory is the experience log itself.
- `similar` ranks past experiences in the same regime by RMS distance over the
  normalised feature vector. It uses only the dimensions both sides have, and needs
  at least MIN_DIMENSIONS of them. The regime flags are left out of the distance:
  the regime filter already matched them, so they would count as free agreement.
- An experience is a candidate only if its label was already known at `as_of` (which
  also means it happened before), so a replay never sees a future outcome.
- Similarity = 1 / (1 + distance), in (0, 1]. It describes how alike the features
  are, never a chance of winning (SIMILARITY_SEMANTICS travels with every match).
- `summarize` is semantic memory, point-in-time, per (strategy, regime). REALIZED
  results (the bot's own, USD) and MARKET_MOVE outcomes (price units) are kept apart.
  A market move counts as "with the side" when the market went the way the decision
  leaned.
- `rag_context` renders matches as a deterministic, cited text block for M03's
  frozen LLM (LRN-01: the model's weights are never changed by trades). Each line
  states the decision's side and whether the outcome went its way.
A linear scan is enough at MVP volumes. A vector index is an ADV performance step;
DTW is CAT-A04-02 (planned).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.enums import Direction
from app.learning.experience import (
    RANGE_M15,
    TREND_M15,
    Experience,
    ExperienceLog,
    Label,
    LabelKind,
)

D = Decimal
MIN_DIMENSIONS = 2
REGIME_KEYS = frozenset({TREND_M15, RANGE_M15})
SIMILARITY_SEMANTICS = "similarity of market features (1 = identical), not a probability of winning"


@dataclass(frozen=True)
class Match:
    similarity: Decimal
    dimensions: int
    experience: Experience
    label: Label
    semantics: str = SIMILARITY_SEMANTICS

    @property
    def decision_id(self) -> str:
        return self.experience.decision_id


@dataclass(frozen=True)
class Summary:
    count: int
    realized: int
    realized_wins: int
    mean_realized: Decimal | None
    """Mean REALIZED final net (USD); None when there is none."""
    moves: int
    moves_with_side: int
    """MARKET_MOVE outcomes that went the way the decision leaned."""
    mean_move: Decimal | None
    """Mean raw MARKET_MOVE (price units, side-independent); None when there is none."""


@dataclass(frozen=True)
class RagContext:
    text: str
    refs: list[str]


def with_side(exp: Experience, label: Label) -> bool | None:
    """Did the outcome go the decision's way? REALIZED: a profit. MARKET_MOVE: the
    market moved in the decision's leaning direction. None when the decision had no
    side or the market did not move."""
    if label.kind is LabelKind.REALIZED:
        return label.value > 0
    if exp.direction in (None, Direction.NEUTRAL) or label.direction is Direction.NEUTRAL:
        return None
    return label.direction is exp.direction


def _distance(a: dict[str, Decimal | None], b: dict[str, Decimal | None]) -> tuple[Decimal, int]:
    diffs = [x - y for k, x in a.items()
             if k not in REGIME_KEYS and x is not None and (y := b.get(k)) is not None]  # fmt: skip
    if not diffs:
        return D(0), 0
    return (sum((d * d for d in diffs), D(0)) / len(diffs)).sqrt(), len(diffs)


def similar(
    log: ExperienceLog,
    vector: dict[str, Decimal | None],
    *,
    regime: str,
    as_of: datetime,
    k: int,
) -> list[Match]:
    if k < 1:
        raise ValueError("k must be at least 1")
    scored: list[Match] = []
    for exp, label in log.known(as_of=as_of, regime=regime):
        distance, dims = _distance(vector, exp.vector)
        if dims < MIN_DIMENSIONS:
            continue
        similarity = (1 / (1 + distance)).quantize(D("0.0001"))
        scored.append(Match(similarity, dims, exp, label))
    scored.sort(key=lambda m: (-m.similarity, m.experience.as_of, m.decision_id))
    return scored[:k]


def summarize(log: ExperienceLog, *, as_of: datetime) -> dict[tuple[str | None, str], Summary]:
    groups: dict[tuple[str | None, str], list[tuple[Experience, Label]]] = {}
    for exp, label in log.known(as_of=as_of):
        groups.setdefault((exp.strategy_id, exp.regime), []).append((exp, label))
    out: dict[tuple[str | None, str], Summary] = {}
    for key, pairs in groups.items():
        realized = [lab.value for _, lab in pairs if lab.kind is LabelKind.REALIZED]
        moves = [(e, lab) for e, lab in pairs if lab.kind is LabelKind.MARKET_MOVE]
        move_values = [lab.value for _, lab in moves]
        out[key] = Summary(
            count=len(pairs),
            realized=len(realized),
            realized_wins=sum(v > 0 for v in realized),
            mean_realized=sum(realized, D(0)) / len(realized) if realized else None,
            moves=len(moves),
            moves_with_side=sum(with_side(e, lab) is True for e, lab in moves),
            mean_move=sum(move_values, D(0)) / len(moves) if moves else None,
        )
    return out


def rag_context(matches: list[Match]) -> RagContext:
    if not matches:
        return RagContext("No similar past experience is known yet.", [])
    lines = [f"Similar past situations ({SIMILARITY_SEMANTICS}):"]
    refs: list[str] = []
    went_text = {True: "went the decision's way", False: "went against the decision",
                 None: "no side to judge"}  # fmt: skip
    for m in matches:
        ref = f"experience:{m.decision_id}"
        refs.append(ref)
        e, lab = m.experience, m.label
        unit = "USD final realized net" if lab.kind is LabelKind.REALIZED else "price move"
        lines.append(
            f"- {ref} at {e.as_of.isoformat()}: {e.action} leaning {e.direction or 'none'}"
            f" by {e.strategy_id or 'no strategy'} in {e.regime}; similarity {m.similarity}"
            f" over {m.dimensions} features; outcome {lab.value} {unit} ({lab.direction}),"
            f" {went_text[with_side(e, lab)]}"
        )
    return RagContext("\n".join(lines), refs)
