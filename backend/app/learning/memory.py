"""Point-in-time similarity retrieval, semantic memory and RAG context (#60; LRN-01,
LRN-02, LRN-03; AT-15; CAT-A04-01, CAT-A04-03).

- Episodic memory is the experience log itself.
- `similar` ranks past experiences in the same regime by Euclidean distance over the
  normalised feature vector. It uses only the dimensions both sides have, and needs
  at least MIN_DIMENSIONS of them.
- An experience is a candidate only if its label was already known at `as_of` (which
  also means it happened before), so a replay never sees a future outcome.
- Similarity = 1 / (1 + distance), in (0, 1]. It describes how alike the features
  are, never a chance of winning (SIMILARITY_SEMANTICS travels with every match).
- `summarize` is semantic memory: counts and mean outcome per (strategy, regime),
  point-in-time.
- `rag_context` renders matches as a deterministic, cited text block for M03's
  frozen LLM (LRN-01: the model's weights are never changed by trades).
A linear scan is enough at MVP volumes; DTW and a vector index are ADV (CAT-A04-02).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.learning.experience import Experience, ExperienceLog, Label, LabelKind

D = Decimal
MIN_DIMENSIONS = 2
SIMILARITY_SEMANTICS = "similarity of market features (1 = identical), not a probability of winning"


@dataclass(frozen=True)
class Match:
    decision_id: str
    similarity: Decimal
    dimensions: int
    experience: Experience
    label: Label
    semantics: str = SIMILARITY_SEMANTICS


@dataclass(frozen=True)
class Summary:
    count: int
    up: int
    down: int
    mean_move: Decimal | None
    """Mean MARKET_MOVE value (price units); None when no market-move label."""
    mean_realized: Decimal | None
    """Mean REALIZED final net (USD); None when no realized label."""


@dataclass(frozen=True)
class RagContext:
    text: str
    refs: list[str]


def _known(log: ExperienceLog, as_of: datetime) -> list[tuple[Experience, Label]]:
    labels = log.labels(as_of=as_of)
    return [(e, labels[e.decision_id]) for e in log.experiences()
            if e.decision_id in labels]  # fmt: skip


def _distance(a: dict[str, Decimal | None], b: dict[str, Decimal | None]) -> tuple[Decimal, int]:
    dims = [(x, b[k]) for k, x in a.items() if x is not None and b.get(k) is not None]
    total = sum(((x - y) ** 2 for x, y in dims if y is not None), D(0))
    return total.sqrt(), len(dims)


def similar(
    log: ExperienceLog,
    vector: dict[str, Decimal | None],
    *,
    regime: str,
    as_of: datetime,
    k: int,
) -> list[Match]:
    scored: list[Match] = []
    for exp, label in _known(log, as_of):
        if exp.regime != regime:
            continue
        distance, dims = _distance(vector, exp.vector)
        if dims < MIN_DIMENSIONS:
            continue
        similarity = (1 / (1 + distance)).quantize(D("0.0001"))
        scored.append(Match(exp.decision_id, similarity, dims, exp, label))
    scored.sort(key=lambda m: (-m.similarity, m.experience.as_of, m.decision_id))
    return scored[:k]


def summarize(log: ExperienceLog, *, as_of: datetime) -> dict[tuple[str, str], Summary]:
    groups: dict[tuple[str, str], list[Label]] = {}
    for exp, label in _known(log, as_of):
        groups.setdefault((exp.strategy_id or "-", exp.regime), []).append(label)
    out: dict[tuple[str, str], Summary] = {}
    for key, labels in groups.items():
        moves = [lab.value for lab in labels if lab.kind is LabelKind.MARKET_MOVE]
        realized = [lab.value for lab in labels if lab.kind is LabelKind.REALIZED]
        out[key] = Summary(
            count=len(labels),
            up=sum(lab.value > 0 for lab in labels),
            down=sum(lab.value < 0 for lab in labels),
            mean_move=sum(moves, D(0)) / len(moves) if moves else None,
            mean_realized=sum(realized, D(0)) / len(realized) if realized else None,
        )
    return out


def rag_context(matches: list[Match]) -> RagContext:
    if not matches:
        return RagContext("No similar past experience is known yet.", [])
    lines = [f"Similar past situations ({SIMILARITY_SEMANTICS}):"]
    refs: list[str] = []
    for m in matches:
        ref = f"experience:{m.decision_id}"
        refs.append(ref)
        e, lab = m.experience, m.label
        lines.append(
            f"- {ref} at {e.as_of.isoformat()}: {e.action} by {e.strategy_id or '-'} in"
            f" {e.regime}; similarity {m.similarity} over {m.dimensions} features;"
            f" outcome {lab.kind} {lab.value} ({lab.direction})"
        )
    return RagContext("\n".join(lines), refs)
