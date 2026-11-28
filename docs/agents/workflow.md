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
| Before merge / ship | OpenCode review (CI) | Every PR. It replaces the subagent `/code-review` + `/scrutinize` self-review. Risk paths also need one independent subagent review; when OpenCode is down, a subagent self-review plus both markers. See "Review gates" below |
| Touching a security boundary | `/security-review` | Every change crossing auth, secrets, broker credentials, the MT5/EA bridge, or LLM/news input handling |
| After implementation | `/verify` | Confirm the feature works in the app |

## OpenCode (read-only)

OpenCode is the reviewer of every PR (see "Review gates" below) and an assistant. It **never changes code**: no job has `contents: write`, and none commits, pushes, opens PRs or edits files. Claude is the developer.

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

### The review format

The steps before `gh pr merge` are in "Review gates → The review → fix loop" below. The review summary has one section per skill that ran, a `Skipped: - <skill> — <reason>` list, and a final line in this exact format: **`Verdict: <n> blocker, <n> major`**. `post.py` computes that line. It counts every `[blocker]`/`[major]` tag anywhere in the summary and in every inline comment body, including inline blocks that were not posted. A failed t4-gates row counts as a major, or as a blocker for gate 10 (the never-list). It overcounts and never undercounts. A review missing a `### <skill>` section for any skill that ran, or missing the `Skipped:` line, is not posted (cause `unavailable: malformed review`). Any model-written `Verdict:` is renamed `Model-verdict:`. The merge gate (xeno-skills #441) parses it. Do not change the format without changing the gate. The skill set and its triggers follow the owner's map in xeno-skills #440. On risk paths (the `riskPaths` globs in `.claude/t4.json`, the one list of record), one independent subagent review is still needed on top of OpenCode.

The review is not advisory. It replaces the subagent `/code-review` + `/scrutinize` self-review (see "Review gates"). TDD, `/verify` and the in-session `/simplify` / `/security-review` triggers stay the agent's own work. Issues filed by the audit enter the normal triage flow through `needs-triage`. `opencode-triage` skips bot-authored issues, so audit issues are triaged by hand.

### Run budgets (liveness watchdog)

A fixed per-attempt `timeout` killed big reviews that were still working: PR #156 (+2023/−148) timed out in all 3 run attempts at 10 minutes (status 124), mid-way through tool calls. `.github/actions/opencode-run/watchdog.py` replaced it (#160). It runs `opencode run` in its own process group, inside the same scrubbed env, writes the `--format json` events to `$RUNNER_TEMP/opencode-events.jsonl`, and passes stderr through. Any new byte on either counts as progress. It stops OpenCode (TERM to the whole group, then KILL after 30 s) in two cases only:

- **Idle** (`idle-minutes`, default 4): no progress for that long. Cause `unavailable: OpenCode stalled (no progress for N min)`.
- **Hard cap** (`hard-minutes`): the budget of the whole OpenCode run (all attempts, the no-answer retry included; install, sign-in and posting come on top), however chatty it is. A retry gets only what is left of it; the cause names the configured cap. Cause `unavailable: OpenCode hit the hard cap (N min)`.

Neither is retried; a policy breach still outranks both (`extract.py` owns the cause). Every minute the log shows `heartbeat: <elapsed> elapsed, <n> events, last progress <t> ago`, never event content. The `tool:`/`usage:` lines appear only after the run, because `extract.py` prints them from the event file. Each attempt adds one line to the job summary: outcome, duration, event count, longest silence, budgets and token usage.

Why 4 minutes idle: OpenCode 1.19.6 in `--format json` emits a `step_start` when a model turn starts, a `tool_use` when each tool call completes, and a `text` only when a text part is complete. Streamed tokens are not emitted. A healthy run is therefore silent for one whole model turn at most, typically the final answer: a few thousand output tokens over a large context. We have no measured gaps yet, because events were never timestamped. 4 minutes is a margin over that turn, not a measurement. It still stops a dead connection 6 minutes sooner than the old 10-minute cap. Tune it from the summary's **longest silence**: raise it if healthy runs come within a minute of it.

| Workflow | idle | hard cap | job `timeout-minutes` |
|---|---|---|---|
| `opencode-review.yml` | 4 | 20 (successful reviews took 5–10 min; #156 needed more than 10) | 30 |
| `opencode-ask.yml` | 4 | 9 | 14 |
| `opencode-audit.yml` | 4 | 15 | 20 |
| `opencode-dispatch.yml` | 4 | 12 | 17 |
| `opencode-triage.yml` | 4 | 7 | 12 |

Each job timeout must exceed the hard cap plus setup (a test checks at least 5 minutes of headroom). Otherwise GitHub cancels the job before the notice step runs. `timeout-minutes` on the action is a deprecated alias of `hard-minutes`, kept for workflow files that predate it. Its meaning changed: it was per attempt and now covers all attempts, so an old caller with `retries: 1` gets one budget, not two; the action logs a deprecation warning when only `timeout-minutes` is set. On a PR the workflow file comes from the PR, but the action from the base revision, so a PR that changes these inputs is reviewed with the base action's budgets.

### When OpenCode is unavailable

Infrastructure failures do **not** turn the `review` check red. These include a model 429, a Console sign-in or OpenCode App token failure, a stall or the hard cap, a malformed review, and an empty answer. The run and post steps use step-level `continue-on-error`. A final step records the cause in the job summary and as a plain **issue comment** on the PR: `OpenCode unavailable: <cause>`. It then exits 0. That notice is never a PR review, because a bot review at the head with no threads could pass the merge gate. The merge-gate hook (xeno-skills #441, OrionFX PR #150) is the real gate. It needs green CI to reach its fallback path, where the developer runs the same skills through subagents.

The check fails **red** only on config or security causes: an empty client-id variable, a missing `id-token` permission, a tool-policy canary breach, an App token not scoped to exactly this repo, or GitHub refusing the notice itself for permissions (HTTP 401, or `Resource not accessible by integration`; a rate-limit 403 stays green). A refused notice is red so that a PR never has neither a review nor a notice behind a green check. The run writes the cause as `<category>: <detail>` to `$RUNNER_TEMP/opencode-cause`, and `post.py classify` reads it. A missing or unknown cause counts as `unavailable`.

The `review` check will **not** be made a required check. The hook is the gate.

### Honest limits

- It is not a required check (an owner decision; see "Fallback, CI and the ruleset" below). A green check can still carry findings, so read the review. A **skipped** check means no review ran. Fork PRs get no review: `opencode-review` and `/oc` on review comments skip them in the `if:`. `/oc` on an *issue comment* in a fork PR does run, from `main` and only for a collaborator commenter. The fork's diff is then only data, and no token reaches the model.
- The repo is **public**. Text in issues, PR bodies and comments may come from anyone, and can bias **what the answer says**. It cannot give the model new tools. The no-exfiltration claim is scoped to that case: an outsider's text cannot make the model read secrets or reach the network. A collaborator who edits the workflow or the shared action in a PR can, as with any workflow.
- The **skills** (`.opencode/skills/`) are read from the PR head, so a PR can change its own reviewer's instructions. `security-review` therefore triggers on `.opencode/`. The token-holding **scripts** run from the base revision (see above). The one exception is the bootstrap PR that introduces them, while `main` has none yet. The workflow file itself always comes from the PR head on `pull_request`, so a collaborator who edits it is not stopped. The collaborator-only guard covers that case.
- **Free models are refused from CI** (`429 FreeUsageLimitError`, 2026-11-28; that run hung for 18 minutes). Billing goes to the owner's Go plan. Every workflow has a hard `timeout-minutes`.
- `opencode-go/deepseek-v4-pro` is the stronger choice, but it returned 403 "Model access is disabled" for this workspace. Enable it in the Console's Go model access before switching to it.
- Workflows on `issue_comment`, `issues`, `schedule` and `workflow_dispatch` run from `main`, so they work only after merge.

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).

## Review gates — OpenCode merge gate (#149, owner decision 2026-11-28)

This section is the before-merge review policy, and the "Before merge / ship" row in the table above points here. It also supersedes any wording elsewhere in this file, or in the OpenCode workflows' own text, that calls the OpenCode review advisory only.

**The hook is the real gate.** `.claude/hooks/orionfx-opencode-merge-gate` enforces this section on Bash, PowerShell and Monitor commands, and on GitHub MCP merge tools. It is on when `"requireOpenCodeReview": true` is set in the working-tree `.claude/t4.json` **or** in the default branch's copy on GitHub, which the hook reads with `gh api`. When the default branch has it on, the hook uses that copy's settings (bot login, `riskPaths`, cap). So an uncommitted edit or an old worktree cannot switch the gate off.

### Who reviews

| Case | Review | Record the gate checks |
|---|---|---|
| Every PR | OpenCode: the `review` check, posted by `opencodeBotLogin` (`opencode-agent[bot]`) | The latest review at the head SHA ends with `Verdict: 0 blocker, 0 major`. Every thread the bot opened on the PR, in any round, is resolved and either answered by you or outdated. |
| A changed file (or a rename's old path) matches `riskPaths` | OpenCode **plus** one independent subagent review | PR comment whose first line is `<!-- t4:independent-review head=<sha> -->`, followed by the findings and how each was handled |
| OpenCode is down: model 429, App token failure, timeout, or the `review` check finished without a review for an infra reason | Fallback self-review with subagents: `code-review`, `scrutinize`, `simplify`, plus `security-review` when relevant | PR comment whose first line is `<!-- t4:opencode-fallback head=<sha> reason="<cause>" -->`, **and** the independent-review comment, both at the head SHA. The hook also requires a **completed** `review` check run at the head (any conclusion, including skipped for Dependabot), so the fallback cannot be used while OpenCode is still running |

Outside these cases, the agent does not spawn subagents to review its own work. TDD, `verify`, and the in-session `simplify` / `security-review` triggers stay the agent's own work.

### The only merge command that passes

```
gh pr merge <number|PR URL> --squash --match-head-commit <head sha>
```

`--squash` is the house convention. The hook does not enforce the merge strategy: `--merge`, `--rebase` or no strategy flag also pass its parse. A trailing `# comment` is allowed, even one that mentions a merge: comments (outside quotes, at a word start) are dropped before the keyword scan. Comments inside a heredoc body that is scanned as code still count. Run it as **the only command in the tool call**: no `;`, `&&`, `||`, `|` or newline before or after it. Another statement in the same call could change which `gh` runs (`export PATH=…`, `hash -p`, `alias`, `cd`). The gate refuses, fail-closed, any other text that looks like a merge. That text is `pr merge`, `/pulls/<n>/merge`, `mergePullRequest` or `enablePullRequestAutoMerge`. The gate counts these keywords three ways: in the raw text, in a copy with quotes and escapes stripped, and in the lexed words. So `gh pr 'merge'`, `m''erge`, `me\rge` and a mid-word line continuation all count. The refusal covers:
- wrappers and prefixes: `bash -c`, `eval`, `xargs`, `{ …; }`, `if …; then`, `time`, `env`, `command`, `VAR=x`;
- other routes: aliases, `gh -R x pr merge`, `gh api … merge`, two merges in one command;
- anything but a plain word after `gh [-R <repo>] pr`, such as `gh pr $'…'`, `gh pr -R x merge` or `gh pr $SUB`.

It also refuses a missing or branch selector, a missing or stale `--match-head-commit`, `--auto` in any spelling (`--auto`, `--auto=…`), and `--admin`. Only known `gh pr merge` flags pass: `--squash`/`--merge`/`--rebase`, `--delete-branch`, `--disable-auto`, `--subject`, `--body`, `--body-file`, `--author-email`, `--repo` and their short forms. The shell must not be able to build arguments the gate did not see. So, outside quotes, the merge command may not contain `$`, a backtick, `{ }`, `* ? [ ]`, `( )` or a PowerShell `@splat`. A line continuation that changes the words is refused too. The same expansion ban applies to any merge-like command (a `merge` word with `pr` or `gh`, or a merge keyword), even one the keyword scan cannot see: `gh${IFS}pr${IFS}merge` or `gh $x merge` is refused. `git merge $BRANCH` is not merge-like and is not touched. Put a subject with parentheses in quotes.

`--match-head-commit` makes GitHub refuse the merge if someone pushed after the check. Text that only *mentions* a merge, such as a PR body or a commit message, belongs in a file (`--body-file`, `-F`) or a heredoc.

**Criterion change to #149 (owner decision relayed 2026-11-28; recorded on #149).** The issue's acceptance says "never touch non-merge commands". The gate instead refuses a non-merge command whose own text contains a merge keyword, such as `rg 'pr merge' docs/`, `git log --grep='pr merge'` or `git commit -m '… pr merge …'`. Telling a mention from a disguised merge needs the full shell grammar, and every partial rule tried in PR #150 had a bypass, so the gate fails closed on purpose: failing closed is cheap, and a heredoc or `--body-file` avoids it. Commands that do not contain the keywords are never touched.

**Heredoc and here-string bodies.** A body is data only when every command in the call is a known data sink: `cat`, `tee`, `git`, `echo`, `printf`, `gh` (except `gh api`, whose `--input -` can carry a merge mutation), `Set-Content`/`Add-Content`/`Out-File`/`Write-Output`, or a variable assignment. No word may name an interpreter either. Anything else makes the bodies code, and they are scanned: `bash <<X`, `$SHELL <<X`, `/bin/ash <<X`, `make -f - <<X`, `cat <<X | sh`, `@'…'@ | iex`. The detection is quote- and comment-aware: `<<EOF` inside quotes or a comment is not a heredoc, and the rest of a `<<EOF` line is still code.

**Plain `gh` only.** The merge must use the bare word `gh`, not a path or a PowerShell call operator. The gate checks the review with the `gh` it finds on PATH, and a merge through another binary might not be that program.

### The review → fix loop

1. **Push.** OpenCode reviews the new head. Wait for it: `gh pr checks <n> --watch`.
2. **Read the latest review at the current head:** `gh api repos/Slow-Inc/OrionFX/pulls/<n>/reviews`, author `opencode-agent[bot]`, `commit_id` = head SHA. Read its summary, its last line `Verdict: <n> blocker, <n> major`, and its inline threads. A finding can live only in the summary, because inline comments are capped at 15.
3. **Handle every finding.** Fix it if it is valid. Otherwise reply on its thread with the reason. Then **resolve the thread**. To pass, a resolved thread needs your reply, or it must be outdated because the fix changed its line. Get thread ids from `pullRequest.reviewThreads { id isResolved }` in GraphQL. Resolve with `gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}}}'`.
4. **If you pushed a fix, go back to step 1.** The new head needs its own review with `Verdict: 0 blocker, 0 major`. Threads from older rounds still count, so a new commit cannot hide an old finding.
5. **Merge only when the gate passes** with the command above:
   - the latest review at the current head has verdict 0/0;
   - no bot thread is unresolved or resolved silently;
   - on a risk-path PR, an independent-review comment names the current head. Re-post it after each push, covering the new commits.
6. **Round cap.** `opencodeMaxRounds` in `.claude/t4.json` (default 5) caps the loop. The gate counts OpenCode reviews on the PR, one per distinct `commit_id`. If the count is above the cap and the gate still fails, it blocks with **stop and ask the owner**. Do not push another round. Report the open findings to the owner and wait for a decision.
7. **Repeat rule.** If the same finding comes back a third time, also stop and ask the owner, even below the round cap. The hook does not detect repeats, so this rule is the agent's own procedure.

### Markers

Post each marker as the **first line** of its own comment, from your own account, with `gh pr comment <n> --body-file <file>`. The gate ignores a marker inside a code fence, a blockquote or later lines. It accepts a marker only when all of these hold:
- The author is not a bot, and their `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`. The repo is public, so anyone else's comment is ignored.
- The SHA is the full 40-character head SHA (`gh pr view <n> --json headRefOid`).
- The fallback `reason` is a real cause, not empty and not `...`.
- The independent-review comment has text below the marker line.

The gate's block message prints the markers with placeholders only. Pasting it into a comment satisfies nothing.

```
<!-- t4:independent-review head=<40-char head sha> -->
Independent review (subagent: code-review + scrutinize) of <sha>:
1. <finding> — fixed in <commit>
2. <finding> — not fixed: <reason>
```

```
<!-- t4:opencode-fallback head=<40-char head sha> reason="model 429 FreeUsageLimitError on run <id>" -->
```

### Fallback, CI and the ruleset

- **OpenCode infra failures finish green.** When OpenCode fails for an infrastructure reason, its job finishes green with an "unavailable" notice (see "When OpenCode is unavailable" above), so the vendored `t4-gate` (`requireGreenCI`) does not block the fallback. This shipped with PR #144, which is on `main` as e885f0e.
- **Transition.** OpenCode reviews every PR now. The hook enforces this section only after PR #150 is merged and `requireOpenCodeReview` is set to `true` in a follow-up. Until then, follow it by procedure.
- **The `review` check is not a required check (owner decision 2026-11-28).** The check finishes green on infra failure, so making it required would not by itself block the fallback. But a required green check gates nothing on its own, because the review is a `COMMENT` and the job exits 0 whatever the verdict. A server-side gate for web merges would need a check that fails on a non-zero verdict, made required, with the fallback path designed around it. That is an open owner decision (#166), not part of this PR. Until then the hook is the gate, for the agent only.
- **The ruleset is live (checked 2026-11-28).** The repo is public. Ruleset `main-protection` (id 28272751) is active on the default branch with no bypass actors. It requires a PR and the `t4-verify` checks `guards`, `lint`, `typecheck`, `test` and `frontend`, and it blocks deletion and force-push. So `git push … :main` is refused server-side, not by this hook. Secret scanning and push protection are on.
- **Dependabot PRs get no OpenCode review**, because the review job skips bot authors. To merge one, run the fallback self-review and post both markers. No Dependabot exemption exists in the hook yet: Dependabot can change `.github/workflows/`, a risk path. An exemption that mirrors the `guards` one is an owner decision.

### `T4-Gates` trailer

`code-review=ran` (and `scrutinize=ran`) means OpenCode reviewed the work, or the fallback subagent review ran. The trailer is written when you commit, usually before OpenCode has run, so `not-run` is the honest value then. The merge gate enforces the review; the trailer does not.

### Limits

- The hook binds Claude only. A human merging on the web is held only by the ruleset's required checks, not by the review gate.
- **How it blocks.** The hook prints a PreToolUse `deny` JSON on stdout and exits 0, the same way `t4-gate` does. It never blocks by exit code. On Windows, the vendored `run-hook.cmd` runs `exit /b %ERRORLEVEL%` inside a `( … )` block, which expands at parse time to 0, so an exit-2 block would be lost. Allow means exit 0 with empty stdout.
- Fail-closed paths, all with the deny JSON:
  - a `gh` or API error, GraphQL `errors`, or a runtime error in the checker;
  - any unexpected exit or signal in the wrapper (a trap);
  - checker output that is not exactly one deny object;
  - perl missing;
  - the 100-second budget for `gh` calls running out;
  - the hook timing out (`onFailure: "block"`, timeout 150 s). `onFailure` needs Claude Code ≥ 2.1.295.
- **"Trusted" is identity-based.** A trusted thread reply or marker comes from an `OWNER`, `MEMBER` or `COLLABORATOR` account. The agent posts as the OWNER account too, so a trusted reply does not prove that a human wrote it.
- **What a text hook cannot close.** It cannot see indirect forms: `$(…)`, a variable holding the command, encoded commands (`pwsh -EncodedCommand`, `base64 -d | sh`), or a script written in one tool call and run in another. The ruleset on `main` is the server-side backstop.
- **`run-hook.cmd` fails open when bash is missing.** It exits 0 silently (an upstream defect in the vendored file). This is one more reason the gate relies on the deny JSON plus the ruleset, not on the exit code.
- A PR with more than 100 review threads is blocked, because the gate does not page them.
- **The on/off switch.** The gate is off only when neither the working tree nor the default branch says `"requireOpenCodeReview": true`. With the switch off locally, the hook reads the default branch only when it would otherwise deny a command or call `gh` for a merge, so ordinary commands cost nothing. If the default branch cannot be read at that point, the command is denied. Without perl, a locally-off gate has no opinion.
- **Working-tree registration.** The hook's registration in `.claude/settings.json` is also working-tree. Reading the default branch closes the invisible-disable case (a stale or edited config), not a determined local edit that removes the hook. The ruleset is the backstop.
