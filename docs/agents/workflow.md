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

## OpenCode (advisory, read-only)

OpenCode is a second, advisory reviewer and assistant. It **never changes code**: no job has `contents: write`, and none commits, pushes, opens PRs or edits files. Claude is the developer.

Every job runs the shared action `.github/actions/opencode-run` with the same three-step pattern:

1. **Sign in.** Exchange the job's GitHub OIDC token for a 1-hour OpenCode key, with no repo secret. The step fails fast if the exchange returns nothing.
2. **Run.** Run the pinned CLI (`opencode-ai@1.19.6`) with a deny-by-default tool policy: only `read`, `glob`, `grep` and `list` inside the checkout, plus the `skill` tool. It has no bash, no edit, no web and no reads outside the checkout. The model never gets a GitHub token, and no tool lets it read its env.
3. **Post.** A trusted workflow step posts the answer.

Each job's context is pre-fetched into `.git/opencode-ctx/`. The model is set in one place, the `model` default in that action (`opencode-go/deepseek-v4.1-flash`).

| Workflow | Trigger | Can | Cannot |
|---|---|---|---|
| `opencode-review.yml` | `pull_request` from collaborator authors, non-draft, non-bot | Load the repo skills (`code-review`, `scrutinize`, `simplify`, `t4-gates`) and post ONE review with event `COMMENT`, plus up to 10 inline comments on lines in the diff | Approve, request changes, push, merge |
| `opencode-ask.yml` | A comment starting `/oc` or `/opencode` by OWNER/MEMBER/COLLABORATOR, on `issue_comment` or `pull_request_review_comment` | Read the repo and reply once | Change code. It refuses and says Claude is the developer |
| `opencode-audit.yml` | `schedule` (Mon 02:00 UTC) + `workflow_dispatch` | Review the last week of `main` and check that the ledger and catalog are honest; open ONE bilingual issue labelled `needs-triage` + `repo-ops`, or comment on the open one; post nothing if there are no findings | Close or edit issues |
| `opencode-triage.yml` | `issues: opened` by a trusted author | Add one Component label and one triage role when the issue has none (never `wontfix`); ask for a missing EN/TH part | Close issues, edit the body |
| `opencode-dispatch.yml` | `workflow_dispatch` with `prompt` (and an optional `issue`) | Answer to the job summary or as an issue comment | Change code |

Service accounts (OpenCode Console, created 2026-11-28). Both are OIDC, pinned to repo ID 1473623856, Models: Request only, any IP:

- **"OrionFX PR Review"**, `sk_dcae4ec7-…`: runs from pull requests. It serves `opencode-review` and `/oc` on PR review comments.
- **"OrionFX Automation"**, `sk_a7b3d9af-…`: runs from branch `main` only. It serves `/oc` on issue comments, audit, triage and dispatch.

The client IDs are credential IDs, not secrets. A scanner flagging their `sk_` prefix is a false positive. To revoke access, disable the account in the Console.

### Gate before `gh pr merge`

1. Wait for the `review` check (`gh pr checks <n> --watch`).
2. Read the OpenCode review (`gh api repos/Slow-Inc/OrionFX/pulls/<n>/reviews`, author `github-actions[bot]`, body starting `OpenCode review of <sha>`) and its inline comments. Its `commit_id` must be the current head. After a push, wait for the new review.
3. For each finding: fix it if it is valid, or reply on the PR saying why not.

The review is advisory evidence. It does not replace `/code-review`, `/scrutinize`, `/security-review` or `/verify`. Issues filed by the audit enter the normal triage flow through `needs-triage`. Issues created with `GITHUB_TOKEN` do not trigger `opencode-triage`.

### Honest limits

- It is not a required check (free plan). A green check can still carry findings, so read the review. A **skipped** check means no review ran. Fork PRs get no OIDC token, so they get no review.
- The repo is **public**. Text in issues, PR bodies and comments may come from anyone, and can bias **what the answer says**. It cannot give the model new tools.
- The skills and the shared action are read from the PR head, so a collaborator PR can change its own reviewer. That is no escalation, because such a PR could edit the workflow anyway.
- **Free models are refused from CI** (`429 FreeUsageLimitError`, 2026-11-28; that run hung for 18 minutes). Billing goes to the owner's Go plan. Every workflow has a hard `timeout-minutes`.
- `opencode-go/deepseek-v4-pro` is the stronger choice, but it returned 403 "Model access is disabled" for this workspace. Enable it in the Console's Go model access before switching to it.
- Workflows on `issue_comment`, `issues`, `schedule` and `workflow_dispatch` run from `main`, so they work only after merge.

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).
