---
name: scrutinize
description: Outsider review of an OrionFX PR. Questions whether the change should exist and whether a simpler approach would do, then traces the real code path end to end to check that the change does what it claims. Use when reviewing a PR, plan or design.
---

# scrutinize (OrionFX)

Read the change as an outsider who owes the author nothing. The diff is where you start, not the edge of what you check.

## Steps, in order

1. **Intent.** In one sentence, state what the PR claims to achieve, taken from the PR body and the linked issue. If you cannot state it, that is the first finding.
2. **Should this exist, and is there a simpler way?** Name any smaller or more direct approach that would reach the same goal, such as an existing helper, a config change instead of code, or less surface. Only raise it when the simpler option is concrete.
3. **Trace end to end.** Follow the changed code through its real callers and callees, outside the diff: entry point, then the changed code, then side effects such as persistence, broker or EA bridge, API and UI. Name the files you followed.
4. **Verify the claims.** Check each claim in the PR body ("fixes X", "adds tests for Y", "no behaviour change") against the code and tests. Flag every claim the code does not back up.
5. **Report.** Every finding states what to change, why, and the evidence (path and line, or the traced path).

## Output section

Write this section exactly as follows:

```
### scrutinize: intent / simpler / trace / claims
Intent: <one sentence>
Traced: <files followed>
- [severity] finding — evidence — suggested change
```

Severity is one of `blocker`, `major`, `minor` or `nit`. If nothing is wrong, say so in one line.

Credit: the method follows the idea of the "scrutinize" skill from the 9arm skill set. This text is written independently for this repo.
