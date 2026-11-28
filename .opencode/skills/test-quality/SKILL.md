---
name: test-quality
description: OrionFX test-quality review of a PR that changes code. Checks that every behaviour change is tested through public interfaces, including error and edge paths, and flags branches no test exercises (likely surviving mutants).
---

# test-quality (OrionFX)

## Trigger

Run this skill when the diff changes code under `backend/`, `frontend/src/`, `scripts/` or `migrations/`, or any `.py`, `.ts` or `.tsx` file. Skip it for PRs that only change docs or config, with the reason "no code changes".

## Check

1. **Coverage of change.** Each behaviour change in the diff has a new or changed test under `tests/` or `frontend/tests/`. Name each change that has none.
2. **Behaviour, not implementation.** Tests call public interfaces and assert observable outcomes: return values, persisted rows, emitted events, rendered output. Flag tests that:
   - mock the unit under test;
   - assert only that a call happened;
   - mirror the implementation line by line.
3. **Error and edge paths.** Look for tests of invalid input, empty or `None`, boundaries (0, max, exact thresholds), time zones and UTC, `Decimal` rounding, and the refusal path, where the system declines when uncertain.
4. **Likely surviving mutants.** For each new branch or condition, ask whether any test would fail if the condition were inverted, the boundary moved by one, or the branch deleted. Each branch no test exercises is a finding.

## Output section

```
### test-quality: coverage / behaviour / edges / mutants
- [severity] path:line — untested or weakly tested behaviour — the test that would catch it
```

Use `major` when a safety or risk path is untested, and `minor` or `nit` otherwise. If there are no findings, write `- no findings`.
