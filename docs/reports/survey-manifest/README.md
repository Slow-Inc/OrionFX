# Survey manifest — provenance cache

So a later scan (a new report, an ADR, an audit) does **not** re-read files/issues/PRs that haven't changed.

**Scan procedure:**
1. Open the fragment covering the area to update (see the fragment index below).
2. Before re-reading, check whether the recorded source changed:
   - Code/docs: `git log -1 --format=%H -- <path>` vs stored `last_commit` — equal ⇒ **skip**.
   - Issue/PR: `gh issue view <n> --json updatedAt` vs stored `updated_at` — equal ⇒ skip.
3. If changed, read only the diff (`git diff <last_commit>..HEAD -- <path>`).
4. Update only the changed part and bump `last_commit` / `updated_at`.

## Fragment index

| Fragment | Scope | Status |
|---|---|---|

_No fragments yet._
