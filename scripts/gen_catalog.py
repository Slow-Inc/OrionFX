"""Sync docs/catalog/coverage.json with the master plan §16.3 table.

Catalog facts (capability, owner, phase, class, dependency) come from the plan;
tracked state (coverage, eligibility, refs, blockers) is kept from the existing
file. New rows start `planned` (R/C/D) or `excluded` (X), as §16.2 requires.
Run after the master plan's catalog changes, then review the diff.
"""

import re
from pathlib import Path

from app.core.catalog import COVERAGE_PATH, CoverageEntry, dump_coverage, load_coverage
from app.core.enums import CatalogClass, CoverageState

ROOT = Path(__file__).resolve().parents[1]
ROW = re.compile(
    r"^\| (CAT-[A-Z]\d{2}-\d{2}) \| (.+?) \| (\S+) \| (\S+) \| ([RCDX]) \| (.*) \|$", re.MULTILINE
)


def main() -> None:
    text = (ROOT / "docs/plans/master_planV1.1.md").read_text(encoding="utf-8")
    old = {e.catalog_id: e for e in load_coverage()} if COVERAGE_PATH.exists() else {}
    entries = []
    for m in ROW.finditer(text):
        cid, capability, owner, phase, cls, dependency = (g.strip() for g in m.groups())
        facts = {
            "catalog_id": cid,
            "capability": capability,
            "owner": owner,
            "phase": phase,
            "catalog_class": CatalogClass(cls),
            "dependency": dependency,
        }
        if cid in old:
            entries.append(old[cid].model_copy(update=facts))
        else:
            state = CoverageState.EXCLUDED if cls == "X" else CoverageState.PLANNED
            entries.append(CoverageEntry(**facts, coverage_state=state))
    COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_PATH.write_text(dump_coverage(entries), encoding="utf-8", newline="\n")
    print(f"wrote {len(entries)} catalog rows to {COVERAGE_PATH}")


if __name__ == "__main__":
    main()
