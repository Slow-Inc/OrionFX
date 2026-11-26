"""Sync docs/catalog/coverage.json with the master plan §16.3 table.

Catalog facts (capability, owner, phase, class, dependency) come from the plan;
tracked state (coverage, refs, blockers) is kept from the existing file. New rows
start `planned` (R/C/D) or `excluded` (X), as §16.2 requires. Every row is
re-validated, so a plan change that contradicts tracked state fails here.
Run after the master plan's catalog changes, then review the diff.
"""

from app.core.catalog import (
    COVERAGE_PATH,
    PLAN_PATH,
    CoverageEntry,
    dump_coverage,
    load_coverage,
    parse_plan_rows,
)
from app.core.enums import CoverageState


def main() -> None:
    old = {e.catalog_id: e for e in load_coverage()} if COVERAGE_PATH.exists() else {}
    entries = []
    for facts in parse_plan_rows(PLAN_PATH.read_text(encoding="utf-8")):
        if facts["catalog_id"] in old:
            tracked = old[facts["catalog_id"]].model_dump()
        else:
            excluded = facts["catalog_class"] == "X"
            tracked = {
                "coverage_state": CoverageState.EXCLUDED if excluded else CoverageState.PLANNED
            }
        entries.append(CoverageEntry.model_validate({**tracked, **facts}))
    COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_PATH.write_text(dump_coverage(entries), encoding="utf-8", newline="\n")
    print(f"wrote {len(entries)} catalog rows to {COVERAGE_PATH}")


if __name__ == "__main__":
    main()
