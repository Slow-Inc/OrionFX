---
name: simplify
description: Quality pass over an OrionFX PR covering reuse, simplification, efficiency and altitude. Reports only; it does not hunt for bugs and does not edit files. Use after the correctness review.
---

# simplify (OrionFX)

This is a quality pass, not a bug hunt. Apply the `karpathy-guidelines` skill: the simplest thing that works, and surgical diffs. Report suggestions only and never edit files.

## Look for

- **Reuse:** new code that duplicates an existing helper, model or fixture in `backend/app/` or `frontend/src/`. Name the existing one.
- **Simplification:** abstractions with a single user, unused options or flags, speculative generality, and dead branches that the change itself introduced.
- **Efficiency:** repeated work in loops, N+1 queries, and needless copies of large data such as candles or features. Report these only when they plausibly matter.
- **Altitude:** logic placed in the wrong module or layer, for example risk logic outside M06, or I/O inside a pure core function.
- **Surgical:** unrelated edits, reformatting or "improvements" that no line of the PR's goal needs.

## Output section

Write this section exactly as follows:

```
### simplify: reuse / simplification / efficiency / altitude
- [minor|nit] path:line — suggestion — what it saves
```

If you have no suggestions, write `- no suggestions`.
