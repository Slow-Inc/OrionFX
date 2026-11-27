---
name: t4-gates
description: OrionFX T4 gate checklist for a pull request, covering TDD, behavioural tests, security-review triggers, catalog honesty, the never-list and risk independent of AI. Use as the last review step.
---

# t4-gates (OrionFX)

Check the PR against each item. Mark it `pass`, `fail` or `n/a`, with one line of evidence. These rules come from `CLAUDE.md`.

1. **TDD present.** Behaviour changes come with new or changed tests in `tests/` or `frontend/src/**/*.test.*`. Docs-only and CI-only PRs are `n/a`.
2. **Tests exercise behaviour.** Tests assert observable outcomes through public interfaces. They do not mirror the implementation, over-mock the unit under test, or assert only that a call happened.
3. **Security-review trigger.** If the change touches auth, secrets, broker credentials, the MT5/EA bridge, LLM or news input handling, or `.github/workflows`, the PR must state that a security review ran (the `T4-Gates:` trailer or PR body).
4. **Catalog honesty.** No Catalog Item is reported as delivered unless its Coverage State in `docs/catalog/coverage.json` says so, with evidence. There is no 100% coverage claim.
5. **Never-list.** No real money, real credentials or real account data. No LLM sends broker orders. Manual Positions are never touched. There is no claim of guaranteed profit, and no claim that an Emergency SL or Emergency Stop fills at an exact price.
6. **Risk independent of AI.** AI output only proposes. M06 decides, and the EA is the only trade writer. No path lets model output bypass the M06 veto.
7. **Issue link.** The PR title or body references an issue (`#n`).

## Output section

Write this section exactly as follows:

```
### t4-gates: checklist
| # | Gate | Result | Evidence |
```

Give one row per item above.
