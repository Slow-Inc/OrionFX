---
name: t4-gates
description: OrionFX T4 gate checklist for a pull request, covering issue link, TDD, behavioural tests, security-review trigger, bug-fix records, bilingual body, honest trailer, evidence-backed claims, catalog honesty, the never-list and risk independent of AI. Use as the last review step.
---

# t4-gates (OrionFX)

Check the PR against each item. Mark it `pass`, `fail` or `n/a`, with one line of evidence. The rules come from `CLAUDE.md` and `docs/agents/workflow.md`. A `fail` on items 5–9 or 11 is `major`; a `fail` on 10 is a `blocker`.

1. **Issue link.** The PR title or body references an issue (`#n`), following the order PRD → issues → PR.
2. **TDD present.** Behaviour changes come with new or changed tests in `tests/` or `frontend/tests/`. Docs-only and CI-only PRs are `n/a`.
3. **Tests exercise behaviour.** Tests assert observable outcomes through public interfaces. The `test-quality` section has the detail.
4. **Security-review trigger.** If the change touches auth, secrets, broker credentials, the MT5/EA bridge, LLM or news input handling, `.github/`, `.claude/` or `.githooks/`, the PR states that a security review ran.
5. **Bug-fix record.** A PR that fixes a bug (title `fix…`, or it says "fixes" or "bug") links a post-mortem or engineering record, in `docs/reports/` or the issue, and that record matches the fix. Other PRs are `n/a`.
6. **Bilingual PR body.** English first, then a Thai mirror of the same depth. A Thai section that only summarizes is a `fail`.
7. **Honest trailer.** The `T4-Gates:` trailer or the gates section of the PR body names who reviewed (for example `code-review=ran`, `not-run` or `n-a`). It does not claim a review the PR shows no evidence of.
8. **Claims backed by evidence.** Every "verified", "tested", "passes", "fixed" or "no behaviour change" in the body points to evidence such as a test, a run, a log or a link. "No verdict before evidence."
9. **Catalog honesty.** No Catalog Item is reported as delivered unless its Coverage State in `docs/catalog/coverage.json` says so, with evidence. There is no 100% coverage claim.
10. **Never-list.** No real money, real credentials or real account data. No LLM sends broker orders. Manual Positions are never touched. There is no claim of guaranteed profit, and no claim that an Emergency SL or Emergency Stop fills at an exact price.
11. **Risk independent of AI.** AI output only proposes. M06 decides, and the EA is the only trade writer. No path lets model output bypass the M06 veto.

## Output section

Write this section exactly as follows:

```
### t4-gates: checklist
| # | Gate | Result | Evidence |
```

Give one row per item above. Below the table, add one bullet per `fail`, tagged with its severity, so the verdict counts it: `- [major] gate 6 — Thai section summarizes instead of mirroring`.
