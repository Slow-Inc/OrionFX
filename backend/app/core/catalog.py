"""The V1.1 capability catalog coverage matrix (#21; master plan §16.2-16.4; AT-37).

One record per CAT-* row, kept in `docs/catalog/coverage.json` and synced from the
master plan table by `scripts/gen_catalog.py` (which keeps the tracked state). The
contract test compares it with the master plan, so a row cannot vanish, and a row
cannot claim implemented/tested without references that exist in the repo.
"""

from collections import Counter
from pathlib import Path
from typing import Self

from pydantic import Field, TypeAdapter, model_validator

from app.core.contracts import CATALOG_ID, ContractModel
from app.core.enums import CatalogClass, CoverageState, Eligibility, Mode

__all__ = [
    "COVERAGE_PATH",
    "CatalogClass",
    "CoverageEntry",
    "CoverageState",
    "Eligibility",
    "coverage_summary",
    "load_coverage",
]

COVERAGE_PATH = Path(__file__).resolve().parents[3] / "docs" / "catalog" / "coverage.json"
DELIVERED = (CoverageState.IMPLEMENTED, CoverageState.TESTED, CoverageState.CERTIFIED)


class CoverageEntry(ContractModel):
    catalog_id: str = Field(pattern=CATALOG_ID)
    capability: str
    owner: str
    phase: str
    catalog_class: CatalogClass
    dependency: str
    coverage_state: CoverageState
    eligibility: Eligibility = Eligibility.DISABLED
    allowed_modes: list[Mode] = Field(default_factory=list)
    implementation_refs: list[str] = Field(default_factory=list)
    test_ids: list[str] = Field(default_factory=list)
    """`tests/<path>::<test function>`; checked to exist."""
    blocker_reason: str | None = None
    resolution_owner: str | None = None
    review_trigger: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.catalog_class is CatalogClass.D and Mode.LIVE in self.allowed_modes:
            raise ValueError(f"{self.catalog_id}: research-only rows are never Live")
        if self.coverage_state is CoverageState.EXCLUDED and self.allowed_modes:
            raise ValueError(f"{self.catalog_id}: an excluded row allows no mode")
        return self


_ENTRIES = TypeAdapter(list[CoverageEntry])


def load_coverage(path: Path = COVERAGE_PATH) -> list[CoverageEntry]:
    return _ENTRIES.validate_json(path.read_text(encoding="utf-8"))


def dump_coverage(entries: list[CoverageEntry]) -> str:
    return _ENTRIES.dump_json(entries, indent=2, exclude_defaults=True).decode() + "\n"


def coverage_summary(entries: list[CoverageEntry]) -> dict[str, object]:
    """The §16.4 report: blocked and excluded rows are never counted as delivered."""
    return {
        "total": len(entries),
        "by_class": dict(Counter(e.catalog_class.value for e in entries)),
        "by_state": dict(Counter(e.coverage_state.value for e in entries)),
        "by_phase_delivered": dict(
            Counter(e.phase for e in entries if e.coverage_state in DELIVERED)
        ),
        "delivered": sum(e.coverage_state in DELIVERED for e in entries),
        "blocked": {
            e.catalog_id: e.blocker_reason
            for e in entries
            if e.coverage_state is CoverageState.BLOCKED
        },
    }
