# Agent Workflow

How agents plan and implement in this repo, and which skills to invoke automatically.

## Development workflow

When planning or implementing a feature, follow this order:

1. **`/grill-me`** — stress-test the concept first (interview-style)
2. **`/grill-with-docs`** — challenge the plan against existing ADRs in `docs/adr/` and `docs/plans/master_planV1.1.md`
3. **Survey the change sites** — enumerate every place the change touches, before the plan exists
4. **`/to-prd`** — create a PRD from the grilled plan (one PRD per epic; module PRDs follow the template in master plan §12 at `docs/prd/PRD-XX-*.md`), carrying the survey as its change inventory
5. **`/to-issues`** — break the PRD into GitHub issues on `Slow-Inc/OrionFX` with triage labels (one issue per deliverable)
6. **`/tdd`** — implement test-first, then make the tests pass

Hard ordering: **PRD → issues → PR**. Never open a PR without a referenced issue.

## Auto-triggered skills

| Trigger | Skill | Condition |
|---|---|---|
| Bug / error / stack trace | `/debug-mantra` | Start a debug session every time |
| After fixing a bug | `/post-mortem` | Record root cause + fix + validation |
| After writing or changing code | `/simplify` | Before committing — check over-engineering |
| Editing UI / frontend (M01) | `/impeccable` | Every time a component or CSS is touched |
| Before merge / ship | `/code-review` + `/scrutinize` | Correctness + outsider perspective |
| Touching a security boundary | `/security-review` | Every change crossing auth, secrets, broker credentials, the MT5/EA bridge, or LLM/news input handling |
| After implementation | `/verify` | Confirm the feature works in the app |

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).

## Review gates — OpenCode merge gate (#149, owner decision 2026-11-28)

This section **supersedes** the "Before merge / ship → `/code-review` + `/scrutinize`" row in the table above, and any wording elsewhere in this file that calls the OpenCode review advisory only or says it "does not replace" those gates. `.claude/hooks/orionfx-opencode-merge-gate` enforces it on `gh pr merge` when `.claude/t4.json` has `"requireOpenCodeReview": true`.

### Who reviews

| Case | Review | Record the gate checks |
|---|---|---|
| Every PR | OpenCode: the `review` check, posted by `opencodeBotLogin` (`opencode-agent[bot]`) | A PR review whose `commit_id` is the head SHA, with every thread it opened **resolved** |
| A changed file (or a rename's old path) matches `riskPaths` | OpenCode **plus** one independent subagent review | PR comment `<!-- t4:independent-review head=<sha> -->` followed by the findings and how each was handled |
| OpenCode is down: model 429, App token failure, timeout, or the check is missing or failed for an infra reason | Fallback self-review with subagents: `code-review`, `scrutinize`, `simplify`, plus `security-review` when relevant | PR comment `<!-- t4:opencode-fallback head=<sha> reason="..." -->` **and** the independent-review comment, both at the head SHA |

Outside these cases the agent does not spawn subagents to review its own work. TDD, `verify`, and the in-session `simplify` / `security-review` triggers stay the agent's own work.

### The review → fix loop

1. **Push.** OpenCode reviews the new head. Wait for it: `gh pr checks <n> --watch`.
2. **Read the review at the current head:** `gh api repos/Slow-Inc/OrionFX/pulls/<n>/reviews`, author `opencode-agent[bot]`, `commit_id` = head SHA, plus its inline threads.
3. **Handle every thread.** Fix it if the finding is valid. Otherwise reply on the thread with the reason. Then **resolve the thread**; a reply alone does not pass the gate. Get thread ids from `pullRequest.reviewThreads { id isResolved }` in GraphQL, then resolve with `gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}}}'`.
4. **If you pushed a fix, go back to step 1.** The new head needs its own review. Resolved threads on an older SHA do not count for the new head.
5. **Merge only when the latest review at the current head has no unresolved thread.** On a risk-path PR, the independent-review comment must also name the current head. Re-post it after a push, covering the new commits.
6. **Round cap.** `opencodeMaxRounds` in `.claude/t4.json` (default 5) caps the loop. The gate counts OpenCode reviews on the PR, one per distinct `commit_id`. If the count is above the cap and the gate still fails, it blocks with **stop and ask the owner**. Do not push another round. Report the open findings to the owner and wait for a decision.

### Markers

Post markers from your own account with `gh pr comment <n> --body-file <file>`. The gate accepts a marker only when all of these hold:
- The author is not a bot, and their `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`. The repo is public, so anyone else's comment is ignored.
- The SHA is the full 40-character head SHA.
- The fallback `reason` is not empty.
- The independent-review comment has text besides the marker.

```
<!-- t4:independent-review head=<40-char head sha> -->
Independent review (subagent: code-review + scrutinize) of <sha>:
1. <finding> — fixed in <commit>
2. <finding> — not fixed: <reason>
```

```
<!-- t4:opencode-fallback head=<40-char head sha> reason="model 429 FreeUsageLimitError on run <id>" -->
```

### `T4-Gates` trailer

`code-review=ran` (and `scrutinize=ran`) means OpenCode reviewed the work, or the fallback subagent review ran. The trailer is written when you commit, usually before OpenCode has run, so `not-run` is the honest value then. The merge gate enforces the review; the trailer does not.

### Limits

- The hook binds Claude only. A human merging on the web is not blocked.
- With `requireGreenCI: true`, the vendored `t4-gate` also blocks while any check is red. A **failed** OpenCode `review` check therefore blocks the merge even with a recorded fallback. Re-run the check, or get an owner decision.
- When opted in, merges through `mcp__github__merge_pull_request` are blocked; use `gh pr merge <n>`.
- A PR with more than 100 review threads is blocked, because the gate does not page them.
