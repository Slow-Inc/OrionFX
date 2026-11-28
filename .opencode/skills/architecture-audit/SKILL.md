---
name: architecture-audit
description: OrionFX weekly architecture audit of main (the weekly audit job only). Finds deepening opportunities, duplication across modules, and docs/ledger/catalog drift from the code. Findings only.
---

# architecture-audit (OrionFX)

Use this skill only in the weekly audit, never in a PR review.

## Check

- **Deepening opportunities:** shallow modules whose interface is about as large as their implementation; pass-through layers; one concept split across several modules (M02–M08, X01, X02) that would be simpler behind one interface. Name the seam you would put in place.
- **Duplication across modules:** the same logic, constants or validation repeated in several modules, for example price or time handling or risk checks done outside M06.
- **Docs drift:**
  - `docs/OPEN-WORK-LEDGER.md` rows marked done with no backing code or tests, and open rows the code has already finished;
  - `docs/catalog/coverage.json` states that claim more than the code delivers;
  - ADRs or PRDs that the code contradicts;
  - stale `[PROPOSED]` or TODO items.

Cite each finding as path:line on both sides, for example the doc claim and the code that contradicts it.

## Output section

```
### architecture-audit: deepening / duplication / drift
- [severity] path:line — finding — suggested action
```

If there are no findings, write `- no findings`.
