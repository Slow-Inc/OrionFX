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

`.github/workflows/opencode-review.yml` runs OpenCode on every PR, in the action's normal mode (runtime v1). The reviewer reads `CLAUDE.md`, loads the repo skills in `.opencode/skills/` in this order (`code-review`, `scrutinize`, `simplify`, `t4-gates`), and posts one consolidated PR comment from `github-actions[bot]` whose first line is `OpenCode review of <head SHA>`. Provenance and licences for the skills are in `.opencode/README.md`.

Before `gh pr merge`:

1. Wait for the `review` check to finish (`gh pr checks <n> --watch`).
2. Read the OpenCode comment (`gh pr view <n> --comments`). Its first line must name the current head SHA. After a new push, wait for the new comment. A comment that only carries an error is not a review.
3. For each finding: fix it if it is valid, or reply on the PR saying why not.

It is advisory evidence. It does not replace `/code-review`, `/scrutinize`, `/security-review` or `/verify`.

Why normal mode: the owner chose repo skills over isolation (2026-11-28). The action's review-only mode turns off project skills.

Threat model: the repo is **public**. The v1 handler puts every PR comment and review into the prompt, whoever wrote it, so any GitHub user can attempt prompt injection.

What the agent can reach: `read`, `glob`, `grep` and `list` inside the checkout (the PR head), and the `skill` tool. Nothing else is allowed (`OPENCODE_CONFIG_CONTENT`, deny-by-default): no bash, no edit, no web, and no reads outside the checkout. The process env holds `OPENCODE_API_KEY` and `GITHUB_TOKEN`, but the model has no tool that can read them. A trusted workflow step writes the diff and any linked issues into `.git/opencode-review/` before OpenCode starts.

What blocks what:

- The job token (`contents: read`, `pull-requests: write`) stops push and merge, including the handler's own auto-push when the tree is dirty.
- Approving is off, because the repo setting "Allow GitHub Actions to create and approve pull requests" is disabled.
- The model cannot post extra comments or edit the PR, because it has no shell or network.
- Untrusted comment text can still bias **what the review says**. Treat it as advisory.

Honest limits:

- It is not a required check (free plan), so nothing blocks a merge without it.
- A green check can still carry findings. Read the comment, not the status.
- A **skipped** check means no review ran (draft PR, bot or Dependabot PR, or author not OWNER/MEMBER/COLLABORATOR). Treat it as "no review", not as a pass.
- Fork PRs are excluded.
- The skills are read from the PR head, so a PR can change its own reviewer. Review edits to `.opencode/` with that in mind.
- **Free models are refused from CI** (`429 FreeUsageLimitError`, 2026-11-28, where the run hung for 18 minutes). The job uses `opencode-go/deepseek-v4-pro`, billed to the owner's Go plan. Every review spends that allowance, so keep the prompt lean. Hard caps: 15 minutes for the job, 12 for OpenCode.
- **No repo secret.** The OpenCode Console service account "OrionFX PR Review" (created 2026-11-28 via the Console) trusts GitHub Actions OIDC. It is pinned to `Slow-Inc/OrionFX` (repo ID 1473623856), accepts pull_request runs only, and has the permission Models: Request. The "Sign in to OpenCode" step exchanges the job's OIDC token (`id-token: write`) for a 1-hour key, masks it, and fails fast if the exchange returns nothing. The client ID `sk_dcae4ec7-d3b6-4328-92a0-b14ae60dfad8` is a credential ID, not a secret; a scanner flagging the `sk_` prefix is a false positive. To revoke access, disable the service account in the Console.

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).
