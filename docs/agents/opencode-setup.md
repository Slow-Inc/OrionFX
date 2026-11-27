# OpenCode Go automation: setup guide

Status: **LIVING** (2026-11-28). This guide is for an agent or a developer wiring OpenCode into a T4 repo. It was written from the OrionFX setup (issue #143, PR #144). Keep it in step with `.github/workflows/opencode-*.yml`, and correct it when a step changes.

**What you get.** OpenCode runs as a second-model **reviewer and assistant** that can never change code:

| Job | Trigger | Does | Never |
|---|---|---|---|
| PR review | `pull_request` (opened, synchronize, reopened, ready_for_review) | Posts a PR review (`COMMENT`) with inline comments at the head SHA, using the repo's review skills | approve, request changes, push |
| `/oc` Q&A | A comment starting `/oc` or `/opencode` on an issue or PR | Answers as a comment | change code: it refuses and points at the developer |
| Weekly audit | `schedule` plus `workflow_dispatch` | Scrutinizes `main` and checks the ledger and catalog for honesty, then opens or updates one bilingual issue | edit files |
| Issue triage | `issues: opened` | Applies labels per `docs/agents/triage-labels.md` and asks for a missing language mirror | close issues or edit bodies |
| Ad-hoc run | `workflow_dispatch` with a `prompt` input | Read-only answer, written to the job summary or as an issue comment | edit files |

No job has `contents: write`. The review is **advisory evidence**: it never replaces the T4 gates.

---

## 0. Prerequisites (owner)

- An **OpenCode Go** subscription on the owner's OpenCode Console workspace.
  - Free models are **refused from CI**. A run returned `429 FreeUsageLimitError` ("Free models work in OpenCode"). After 3 retries the process did not exit; it hung for 18 minutes and burned Actions minutes. Always use a Go model.
- Admin rights on the GitHub repo or org.
- `gh` CLI authenticated for the repo.

## 1. Install the OpenCode GitHub App (owner, once per repo or org)

1. Open https://github.com/apps/opencode-agent, then **Install** or **Configure**, and grant the target repo. "All repositories" is acceptable if the owner chooses it.
2. Why it is needed: reviews and comments are posted **as the OpenCode app bot** (an OIDC exchange to an App installation token). Without the App the job fails with `App token exchange failed: 404 … not installed`.
3. Granting a third-party app access to a private repo is an owner decision. An agent must not do it unasked.

## 2. Create two Console service accounts (owner, or the agent via browser with the owner's approval)

Model access uses **GitHub Actions OIDC**: no API key is stored anywhere. Each service account is pinned to the repo's numeric ID and to one kind of run.

In **opencode.ai/console → Settings → Service accounts → Add**:

| Name (example) | Where will it run? | Repository | Runs from | Permissions | Allowed IPs |
|---|---|---|---|---|---|
| `<Repo> PR Review` | GitHub Actions | `owner/repo` | **Pull requests** | Models · Request | blank (runner IPs vary) |
| `<Repo> Automation` | GitHub Actions | `owner/repo` | **Branch** `main` | Models · Request | blank |

Why two accounts:
- `pull_request` runs present the OIDC subject `repo:owner/repo:pull_request`.
- `schedule`, `issue_comment`, `issues` and `workflow_dispatch` run from the default branch and present `ref:refs/heads/main`.
- One account cannot match both.

After **Create**, the Console shows an "Add to your workflow" snippet and a **client ID** (`sk_…`). Copy only the client ID into the workflow.
- The client ID is a **credential ID, not a secret**: it works only together with an OIDC token minted by that repo's matching workflow.
- Add a comment saying so, because the `sk_` prefix trips secret scanners.

Sign-in step (from the Console; give each job its own `client_id`):

```yaml
permissions:
  id-token: write      # OIDC for the Console token AND the OpenCode App token
  contents: read
steps:
  - name: Sign in to OpenCode
    env:
      AUDIENCE: https://opencode.ai/console
      CLIENT_ID: sk_xxxxxxxx-...   # credential ID, not a secret (OIDC-bound)
    run: |
      set -euo pipefail
      oidc=$(curl -sSf --oauth2-bearer "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
        "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$AUDIENCE" | jq -r .value)
      token=$(curl -sSf https://opencode.ai/console/auth/oauth/token \
        -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
        -d subject_token_type=urn:ietf:params:oauth:token-type:jwt \
        -d client_id="$CLIENT_ID" \
        -d subject_token="$oidc" | jq -r .access_token)
      [ -n "$token" ] && [ "$token" != "null" ] || { echo "::error::OpenCode sign-in failed"; exit 1; }
      echo "::add-mask::$token"
      echo "OPENCODE_API_KEY=$token" >> "$GITHUB_ENV"
```

The token lasts one hour. Every run exchanges again.

## 3. Add the workflows and skills (agent)

1. **Action.** Use `dceoy/opencode-action` **pinned by full SHA**, in its **normal mode**.
   - The reusable review-only workflow "disables project configuration and external skill discovery", so it cannot use repo skills.
   - That is a trade-off: isolation versus skills. The owner chooses; OrionFX chose skills.
2. **Identity.** `use-github-token: false`, so output posts as the OpenCode App bot, not `github-actions[bot]`.
3. **Permissions per job.**
   - Top level: `permissions: {}`.
   - PR review: `contents: read`, `pull-requests: write`, `id-token: write`, plus `issues: read` if it reads linked issues.
   - `/oc` and triage: add `issues: write`.
   - Never `contents: write`.
4. **Guards.**
   - `author_association` is OWNER, MEMBER or COLLABORATOR.
   - Skip drafts and bots.
   - Exclude forks with `github.event.pull_request.head.repo.full_name == github.repository`; `author_association` alone does not exclude them.
   - Never use `pull_request_target`.
5. **Checkout** `ref: ${{ github.event.pull_request.head.sha }}` with `persist-credentials: false`. The default merge ref would not match the SHA being reviewed.
6. **Fail closed.**
   - Use `set -euo pipefail` in every shell step and do not use `2>/dev/null`. A failed `gh pr diff` must fail the job; otherwise an empty diff yields a false "no findings".
   - Set a hard `timeout-minutes` on each job and the action.
   - Add a `concurrency` group per PR or issue.
7. **Model.** One `opencode-go/<model>` id, set in **one place** (an env var). It must be a model the Go plan enables. Check the Console → Go page. In OrionFX the docs drifted from the workflow within a single PR.
8. **Review output.** Submit through `POST /pulls/{n}/reviews` with `event: COMMENT` and `commit_id = head SHA`.
   - Inline comments go on the exact lines.
   - The summary body has sections for code-review, scrutinize, simplify and the t4-gates table.
   - Findings that cannot be anchored go in the summary.
9. **Skills.** Put them in `.opencode/skills/<id>/SKILL.md`, with frontmatter `name` and `description`. OpenCode also reads `.claude/skills/`. Add `.opencode/README.md` stating where each skill came from.
   - Vendor the owner's own skills, such as xeno-skills `karpathy-guidelines`, with attribution.
   - Check the licence of third-party skills before vendoring. If it is unclear, write your own.
   - **Never copy Claude Code's bundled skills** (`/code-review`, `/simplify`) verbatim. Write repo-specific equivalents.
10. **Docs.** Add an OpenCode block to `CLAUDE.md` → Enforcement and to `docs/agents/workflow.md`: the jobs, what each may and may not do, the two service accounts and the merge rule. Add a ledger row for any owner step still open.

## 4. Verify

| What | How | When |
|---|---|---|
| PR review | Open the setup PR itself. `gh pr checks <n> --watch`, then `gh api repos/<o>/<r>/pulls/<n>/reviews`. Confirm the review **author is the OpenCode app bot**, that `commit_id` is the head SHA, and that inline comments exist. | Before merge |
| No secret leaks | The job log shows the token masked (`***`). Search the log for `sk_` and confirm only the client ID appears. | Before merge |
| `/oc`, audit, triage, dispatch | These run **only from the default branch**, so they cannot be tested before merge. After merge, comment `/oc what does <file> do?`, open a test issue, and run the audit with `workflow_dispatch`. | After merge |
| Cost | Console → Go: the usage per run. Keep the audit to one weekly run. | First week |

Report honestly what was verified **before** merge and what is still pending **after** merge.

## 5. Merge rule (agent)

Before `gh pr merge`, follow these steps:
1. Wait for the review check to finish.
2. Read the OpenCode review **at the current head SHA**.
3. For each finding, fix it or reply in its thread with the reason it is not fixed.
4. A green check alone is not enough: the job can pass with findings, and a hung or failed run posts nothing.
5. If the run failed for an infrastructure reason (model 429, App missing, timeout), say so in a PR comment and record the gate as `not-run`. Never treat it as passed.

The planned hook enforcement is in xeno-skills #441 (`requireOpenCodeReview`).

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `429 FreeUsageLimitError … Free models work in OpenCode`, then the job hangs | A free model was used from CI | Use an `opencode-go/…` model, and set `timeout-minutes` |
| `App token exchange failed: 404 … not installed` | The OpenCode GitHub App is not on the repo | Step 1 |
| `Model access is disabled` | The model is not enabled for the workspace or plan | Pick a model enabled on the Console → Go page |
| Token exchange returns `null` | The service account's "Runs from" does not match the event (PR account used on `main`, or the reverse) | Use the account that matches the event (step 2) |
| The review is posted by `github-actions[bot]` | `use-github-token: true`, or the job posts with `GITHUB_TOKEN` | Use the App-token flow (step 3.2) |
| Review says "no findings" on a real change | The diff fetch failed silently | Fail closed (step 3.6) |
| `/oc` does nothing on a PR before merge | Comment workflows run only from the default branch | Expected: test after merge |

## 7. Cost and control

- Billing goes to the owner's Go plan allowance. Each review reports `usage.opencode.allowance`.
- To pause everything, disable the workflows: `gh workflow disable <name>`.
- To revoke model access, delete or disable the service accounts in the Console. Without them the OIDC exchange fails, and the jobs fail fast.
