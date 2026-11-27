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
| Before merge / ship | OpenCode review (CI) | Every PR — see below |
| Touching a security boundary | `/security-review` | Every change crossing auth, secrets, broker credentials, the MT5/EA bridge, or LLM/news input handling |
| After implementation | `/verify` | Confirm the feature works in the app |

## OpenCode review gate

`.github/workflows/opencode-review.yml` runs an AI review on every PR and posts one `COMMENT` review from `opencode-agent[bot]`. Before `gh pr merge`:

1. Wait for the `review / opencode-review` check to finish (`gh pr checks <n> --watch`).
2. Read the OpenCode review (`gh pr view <n> --comments`, or `gh api repos/Slow-Inc/OrionFX/pulls/<n>/reviews`). It must be on the current head SHA (`commit_id`); after a new push, wait for the new one.
3. For each finding: fix it if it is valid, or reply on the PR saying why not.

It is advisory evidence. It does not replace `/code-review`, `/scrutinize`, `/security-review` or `/verify`. Honest limits:

- It is not a required check (free plan), so nothing blocks a merge without it.
- A green check can still carry findings. Read the review, not the status.
- A **skipped** check means no review ran (draft PR, bot or Dependabot PR, or author not OWNER/MEMBER/COLLABORATOR). Treat it as "no review", not as a pass.
- Fork PRs are excluded.
- The default free model can change or be withdrawn. To switch to OpenCode Go, add the `OPENCODE_API_KEY` secret and set `model:` in the workflow (see its header).
- It reads only the diff and frozen repo files, not `CLAUDE.md`. The workflow prompt carries the few repo rules it checks.

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).
