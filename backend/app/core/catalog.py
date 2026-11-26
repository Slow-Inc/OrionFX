"""The V1.1 capability catalog coverage matrix (#21; master plan §16.2-16.4; AT-37).

One record per CAT-* row, kept in `docs/catalog/coverage.json` and synced from the
master plan table by `scripts/gen_catalog.py` (which keeps the tracked state). The
contract test compares it with the master plan, so a row cannot vanish, and a row
cannot claim implemented/tested without references that exist in the repo.
Operational eligibility is not tracked here: it belongs to `EligibilityRecord`.
Per-version §16.4 fields (artifact hash, parameter bounds, context, revocation)
belong to the registry records of #20; this matrix is the per-row disposition.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Self, TypedDict

from pydantic import Field, TypeAdapter, model_validator

from app.core.contracts import CatalogId, ContractModel
from app.core.enums import CatalogClass, CoverageState

__all__ = [
    "COVERAGE_PATH",
    "DELIVERED",
    "PLAN_PATH",
    "CoverageEntry",
    "CoverageSummary",
    "coverage_summary",
    "dump_coverage",
    "load_coverage",
    "parse_plan_rows",
]

ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "docs" / "plans" / "master_planV1.1.md"
COVERAGE_PATH = ROOT / "docs" / "catalog" / "coverage.json"
DELIVERED = (CoverageState.IMPLEMENTED, CoverageState.TESTED, CoverageState.CERTIFIED)
FACTS = ("catalog_id", "capability", "owner", "phase", "catalog_class", "dependency")
_ROW = re.compile(
    r"^\| (CAT-[A-Z]\d{2}-\d{2}) \| (.+?) \| (\S+) \| (\S+) \| ([RCDX]) \| (.*) \|$", re.MULTILINE
)


def parse_plan_rows(text: str) -> list[dict[str, str]]:
    """The §16.3 table rows, in plan order: the facts every coverage entry mirrors."""
    return [
        dict(zip(FACTS, (g.strip() for g in m.groups()), strict=True)) for m in _ROW.finditer(text)
    ]


class CoverageEntry(ContractModel):
    catalog_id: CatalogId
    capability: str
    owner: str
    phase: str
    catalog_class: CatalogClass
    dependency: str
    coverage_state: CoverageState
    implementation_refs: list[str] = Field(default_factory=list)
    test_ids: list[str] = Field(default_factory=list)
    """`tests/<path>::<test function>`; checked to exist."""
    blocker_reason: str | None = None
    resolution_owner: str | None = None
    review_trigger: str | None = None
    note: str | None = None

    @property
    def family(self) -> str:
        """`S01` for CAT-S01-02: the §16.3 family the row belongs to."""
        return self.catalog_id.split("-")[1]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        cid, state = self.catalog_id, self.coverage_state
        if (self.catalog_class is CatalogClass.X) != (state is CoverageState.EXCLUDED):
            raise ValueError(f"{cid}: class X rows, and only they, are excluded")
        if state is CoverageState.CERTIFIED:
            raise ValueError(f"{cid}: nothing is certified yet (no qualification exists)")
        if state in (CoverageState.IMPLEMENTED, CoverageState.TESTED) and not (
            self.implementation_refs
        ):
            raise ValueError(f"{cid}: {state} needs implementation refs")
        if state is CoverageState.TESTED and not self.test_ids:
            raise ValueError(f"{cid}: tested needs test ids")
        blocker = (self.blocker_reason, self.resolution_owner, self.review_trigger)
        if state is CoverageState.BLOCKED and not all(blocker):
            raise ValueError(f"{cid}: blocked needs reason, owner and review trigger")
        return self


class CoverageSummary(TypedDict):
    total: int
    by_class: dict[str, int]
    by_state: dict[str, int]
    by_phase_state: dict[str, dict[str, int]]
    by_family_state: dict[str, dict[str, int]]
    delivered: int
    excluded: int
    blocked: dict[str, str | None]


def _grouped(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter[str]] = {}
    for group, state in pairs:
        out.setdefault(group, Counter())[state] += 1
    return {g: dict(c) for g, c in out.items()}


_ENTRIES = TypeAdapter(list[CoverageEntry])


def load_coverage(path: Path = COVERAGE_PATH) -> list[CoverageEntry]:
    return _ENTRIES.validate_json(path.read_text(encoding="utf-8"))


def dump_coverage(entries: list[CoverageEntry]) -> str:
    return _ENTRIES.dump_json(entries, indent=2, exclude_defaults=True).decode() + "\n"


def coverage_summary(entries: list[CoverageEntry]) -> CoverageSummary:
    """The §16.4 report: blocked and excluded rows are never counted as delivered."""
    states = [e.coverage_state.value for e in entries]
    return {
        "total": len(entries),
        "by_class": dict(Counter(e.catalog_class.value for e in entries)),
        "by_state": dict(Counter(states)),
        "by_phase_state": _grouped([(e.phase, e.coverage_state.value) for e in entries]),
        "by_family_state": _grouped([(e.family, e.coverage_state.value) for e in entries]),
        "delivered": sum(e.coverage_state in DELIVERED for e in entries),
        "excluded": states.count(CoverageState.EXCLUDED.value),
        "blocked": {
            e.catalog_id: e.blocker_reason
            for e in entries
            if e.coverage_state is CoverageState.BLOCKED
        },
    }
