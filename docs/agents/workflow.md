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

1. **Install** the pinned CLI (`opencode-ai@1.19.6`) before any key exists in the job, so npm install scripts never see one.
2. **Sign in and run in one step.** Exchange the job's GitHub OIDC token for a 1-hour OpenCode key, with no repo secret. The step fails fast if the exchange returns nothing. The key lives only in that step's process env, never in `GITHUB_ENV`. Then run the CLI with a deny-by-default tool policy: only `read`, `glob`, `grep` and `list` inside the checkout, plus the `skill` tool. It has no bash, no edit, no web and no reads outside the checkout. The model never gets a GitHub token, and no tool lets it read its env. The CLI runs under a liveness watchdog, not a fixed timeout (see **Run budgets** below).
3. **Post.** A trusted workflow step posts the answer **as the OpenCode GitHub App** (`opencode-agent[bot]`). The step gets an App installation token through a second OIDC exchange (audience `opencode-github-action`, separate from the Console model sign-in), uses it for that step's POSTs (one review; or a label call plus a comment in triage), and then revokes it. One helper, `.github/opencode/with-app-token.sh`, does mint, mask, run and revoke for every workflow (exit 3 = token not scoped to this repo, 4 = no token). The exchange endpoint scopes the token to this repository only, but it takes no parameters, so its permissions cannot be narrowed below the installation's. The job's own `GITHUB_TOKEN` is read-only, except `pull-requests: write` on the review job. That permission is used only for the "OpenCode unavailable" notice, which `github-actions[bot]` posts as an issue comment on the PR. On a PR, `POST /issues/<n>/comments` checks `pull-requests`, not `issues`: with `issues: write` and `pull-requests: read` every notice got HTTP 403 (runs 46131122474 and 46131996298, #160). The merge gate counts only `opencode-agent[bot]` reviews, so this token cannot forge a review that passes it. The App token is broader than the job token (it carries the App's installation permissions), so it lives only in the trusted posting step and never reaches the model.

**OpenCode App installation (read from GitHub on 2026-11-28):** `opencode-agent`, repository selection **all**. Permissions: `contents: write`, `issues: write`, `pull_requests: write`, `actions: read`, `metadata: read`. Events: issues, issue_comment, pull_request, pull_request_review, pull_request_review_comment, workflow_job, workflow_run. `contents: write` is broader than we use. Our scripts only POST reviews, comments, issues and labels. The App publisher sets the permissions, so an installer cannot narrow them. The owner can narrow the repository selection (see the ledger).

**Trusted tooling runs from the base revision.** The repo is public, so on PR events (`opencode-review`, and `/oc` on review comments) every script that holds a token runs from the PR's **base** revision, checked out separately into a freshly created `_trusted/` (any `_trusted/` committed in the PR is deleted first). The one exception is the bootstrap PR, while `main` has no tooling yet. This covers the shared action (Console key), `app-token.sh`, `with-app-token.sh` and `post.py` (App token).

The PR head is more than data. OpenCode runs in the PR-head checkout, so it loads that checkout's `.opencode/` skills, and any project config or plugins, as code inside the OpenCode process. That process holds the Console key (`OPENCODE_API_KEY`). It does **not** hold the OIDC request credentials or runtime tokens, which are unset, so it cannot mint an App token. Audit, triage and dispatch run only from `main`. Limit: on a `pull_request` event the workflow file itself still comes from the PR head. This guard therefore stops a PR from shipping a modified poster, but it does not stop a collaborator who rewrites the workflow. Such an edited workflow can call the base `app-token.sh` and get a repo-scoped App token, which carries the App's installation permissions. The trust boundary is write access to the repo: only collaborators can push same-repo branches, and they can already change any workflow. The guards keep everyone else out. They do not constrain collaborators.

Each job's context is pre-fetched into `.git/opencode-ctx/`. The model is set in one place, the `model` default in that action (`opencode-go/deepseek-v4.1-flash`).

| Workflow | Trigger | Can | Cannot |
|---|---|---|---|
| `opencode-review.yml` | `pull_request` from same-repo branches (forks skipped), collaborator authors, non-draft, non-bot | Run the review skills (always `code-review`, `scrutinize`, `simplify`, `t4-gates`; `security-review`, `test-quality` and `ui-audit` when their trigger holds; see `.opencode/README.md`) and submit ONE pull request review (`event: COMMENT`, `commit_id` = head) with a summary that keeps each skill section, plus inline comments on the exact diff lines (at most 15). Findings that cannot be tied to a diff line stay in the summary | Approve, request changes, push, merge |
| `opencode-ask.yml` | A comment starting `/oc ` or `/opencode ` (followed by the question) by OWNER/MEMBER/COLLABORATOR, on `issue_comment` or `pull_request_review_comment` | Read the repo and reply once | Change code. It refuses and says Claude is the developer |
| `opencode-audit.yml` | `schedule` (Mon 02:00 UTC) + `workflow_dispatch` | Review the last week of `main` and check that the ledger and catalog are honest; open ONE bilingual issue labelled `needs-triage` + `repo-ops`, or comment on the open one; post nothing if there are no findings | Close or edit issues |
| `opencode-triage.yml` | `issues: opened` by a trusted author | Add one Component label and one triage role when the issue has none (never `wontfix`); ask for a missing EN/TH part | Close issues, edit the body |
| `opencode-dispatch.yml` | `workflow_dispatch` with `prompt` (and an optional `issue`) | Answer to the job summary or as an issue comment | Change code |

Service accounts (OpenCode Console, created 2026-11-28). Both are OIDC, pinned to repo ID 1473623856, Models: Request only, any IP:

- **"OrionFX PR Review"**, client ID in repo variable `OPENCODE_PR_CLIENT_ID`: runs from pull requests. It serves `opencode-review` and `/oc` on PR review comments.
- **"OrionFX Automation"**, client ID in repo variable `OPENCODE_AUTOMATION_CLIENT_ID`: runs from branch `main` only. It serves `/oc` on issue comments, audit, triage and dispatch.

The client IDs are credential IDs, not secrets: each works only with an OIDC token from this repo on the allowed ref. They live in repo variables, not in code, because secret scanning flagged them as payment keys (both alerts resolved as false positives on 2026-11-28). To revoke access, disable the account in the Console.

### Gate before `gh pr merge`

1. Wait for the `review` check (`gh pr checks <n> --watch`).
2. Read the OpenCode review (`gh api repos/Slow-Inc/OrionFX/pulls/<n>/reviews`, author `opencode-agent[bot]`, body starting `OpenCode review of <sha>`) and its inline comments. Its `commit_id` must be the current head. After a push, wait for the new review.
3. For each finding: fix it if it is valid, or reply on the PR saying why not.

The review summary has one section per skill that ran, a `Skipped: - <skill> — <reason>` list, and a final line in this exact format: **`Verdict: <n> blocker, <n> major`**. `post.py` computes that line. It counts every `[blocker]`/`[major]` tag anywhere in the summary and in every inline comment body, including inline blocks that were not posted. A failed t4-gates row counts as a major, or as a blocker for gate 10 (the never-list). It overcounts and never undercounts. A review missing a `### <skill>` section for any skill that ran, or missing the `Skipped:` line, is not posted (cause `unavailable: malformed review`). Any model-written `Verdict:` is renamed `Model-verdict:`. The merge gate (xeno-skills #441) parses it. Do not change the format without changing the gate. The skill set and its triggers follow the owner's map in xeno-skills #440. On risk paths (money or risk code, secrets, workflows, `.claude/`), one independent subagent review is still needed on top of OpenCode.

The review is advisory evidence. It does not replace `/code-review`, `/scrutinize`, `/security-review` or `/verify`. Issues filed by the audit enter the normal triage flow through `needs-triage`. `opencode-triage` skips bot-authored issues, so audit issues are triaged by hand.

### Run budgets (liveness watchdog)

A fixed per-attempt `timeout` killed big reviews that were still working: PR #156 (+2023/−148) timed out in all 3 run attempts at 10 minutes (status 124), mid-way through tool calls. `.github/actions/opencode-run/watchdog.py` replaced it (#160). It runs `opencode run` in its own process group, inside the same scrubbed env, writes the `--format json` events to `$RUNNER_TEMP/opencode-events.jsonl`, and passes stderr through. Any new byte on either counts as progress. It stops OpenCode (TERM to the whole group, then KILL after 30 s) in two cases only:

- **Idle** (`idle-minutes`, default 4): no progress for that long. Cause `unavailable: OpenCode stalled (no progress for N min)`.
- **Hard cap** (`hard-minutes`): the budget of the whole step, the no-answer retry included, however chatty the run. Cause `unavailable: OpenCode hit the hard cap (N min)`.

Neither is retried; a policy breach still outranks both (`extract.py` owns the cause). Every minute the log shows `heartbeat: <elapsed> elapsed, <n> events, last progress <t> ago`, never event content. The `tool:`/`usage:` lines appear only after the run, because `extract.py` prints them from the event file. Each attempt adds one line to the job summary: outcome, duration, event count, longest silence, budgets and token usage.

Why 4 minutes idle: OpenCode 1.19.6 in `--format json` emits a `step_start` when a model turn starts, a `tool_use` when each tool call completes, and a `text` only when a text part is complete. Streamed tokens are not emitted. A healthy run is therefore silent for one whole model turn at most, typically the final answer: a few thousand output tokens over a large context. We have no measured gaps yet, because events were never timestamped. 4 minutes is a margin over that turn, not a measurement. It still stops a dead connection 6 minutes sooner than the old 10-minute cap. Tune it from the summary's **longest silence**: raise it if healthy runs come within a minute of it.

| Workflow | idle | hard cap | job `timeout-minutes` |
|---|---|---|---|
| `opencode-review.yml` | 4 | 20 (successful reviews took 5–10 min; #156 needed more than 10) | 30 |
| `opencode-ask.yml` | 4 | 9 | 14 |
| `opencode-audit.yml` | 4 | 15 | 20 |
| `opencode-dispatch.yml` | 4 | 12 | 17 |
| `opencode-triage.yml` | 4 | 7 | 12 |

Each job timeout must exceed the hard cap plus setup (a test checks at least 5 minutes of headroom). Otherwise GitHub cancels the job before the notice step runs. `timeout-minutes` on the action is a deprecated alias of `hard-minutes`, kept for workflow files that predate it. Its meaning changed: it was per attempt and is now the whole step, so an old caller with `retries: 1` gets one budget, not two. On a PR the workflow file comes from the PR, but the action from the base revision, so a PR that changes these inputs is reviewed with the base action's budgets.

### When OpenCode is unavailable

Infrastructure failures do **not** turn the `review` check red. These include a model 429, a Console sign-in or OpenCode App token failure, a stall or the hard cap, a malformed review, and an empty answer. The run and post steps use step-level `continue-on-error`. A final step records the cause in the job summary and as a plain **issue comment** on the PR: `OpenCode unavailable: <cause>`. It then exits 0. That notice is never a PR review, because a bot review at the head with no threads could pass the merge gate. The merge-gate hook (xeno-skills #441, OrionFX PR #150) is the real gate. It needs green CI to reach its fallback path, where the developer runs the same skills through subagents.

The check fails **red** only on config or security causes: an empty client-id variable, a missing `id-token` permission, a tool-policy canary breach, an App token not scoped to exactly this repo, or GitHub refusing the notice itself for permissions (HTTP 401, or `Resource not accessible by integration`; a rate-limit 403 stays green). A refused notice is red so that a PR never has neither a review nor a notice behind a green check. The run writes the cause as `<category>: <detail>` to `$RUNNER_TEMP/opencode-cause`, and `post.py classify` reads it. A missing or unknown cause counts as `unavailable`.

The `review` check will **not** be made a required check. The hook is the gate.

### Honest limits

- It is not a required check, and will not become one (see above). A green check can still carry findings, so read the review. A **skipped** check means no review ran. Fork PRs get no review: `opencode-review` and `/oc` on review comments skip them in the `if:`. `/oc` on an *issue comment* in a fork PR does run, from `main` and only for a collaborator commenter. The fork's diff is then only data, and no token reaches the model.
- The repo is **public**. Text in issues, PR bodies and comments may come from anyone, and can bias **what the answer says**. It cannot give the model new tools. The no-exfiltration claim is scoped to that case: an outsider's text cannot make the model read secrets or reach the network. A collaborator who edits the workflow or the shared action in a PR can, as with any workflow.
- The **skills** (`.opencode/skills/`) are read from the PR head, so a PR can change its own reviewer's instructions. `security-review` therefore triggers on `.opencode/`. The token-holding **scripts** run from the base revision (see above). The one exception is the bootstrap PR that introduces them, while `main` has none yet. The workflow file itself always comes from the PR head on `pull_request`, so a collaborator who edits it is not stopped. The collaborator-only guard covers that case.
- **Free models are refused from CI** (`429 FreeUsageLimitError`, 2026-11-28; that run hung for 18 minutes). Billing goes to the owner's Go plan. Every workflow has a hard `timeout-minutes`.
- `opencode-go/deepseek-v4-pro` is the stronger choice, but it returned 403 "Model access is disabled" for this workspace. Enable it in the Console's Go model access before switching to it.
- Workflows on `issue_comment`, `issues`, `schedule` and `workflow_dispatch` run from `main`, so they work only after merge.

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).
