# CLAUDE.md — OrionFX

Agent operating manual. OrionFX is an **agent-primary** T4 repo (Slow-Inc): the coding agent is the main developer, so these docs are its working memory.

## Engineering north-star

A personal XAUUSD AI auto-trading bot on MT5 where **risk is independent of AI**: AI proposes, the Risk & Execution Engine (M06) decides, and the MQL5 Execution EA is the only trade writer. Safety and refusal-when-uncertain outrank profit and availability. Requirements of record: `docs/plans/master_planV1.1.md` — [LOCKED] items change only through a Change Request (§13.1).

**Catalog honesty (V1.1 §16.4):** never report a **Catalog Item** as delivered unless its **Coverage State** says so with evidence; planned/blocked/excluded rows are tracked, not hidden; no 100% coverage claim.

**Never:** use real money, real credentials, or real account data during build/test; let an LLM send broker orders; touch **Manual Positions**; claim guaranteed profit or that an **Emergency SL** / **Emergency Stop** fills at an exact price.

## Standing defaults

**`using-t4` is the standing default for every task in this repo — not a pointer read once.** Route through it before acting, and **re-route at every phase boundary**: after writing code → `simplify`; before merge → `code-review` + `scrutinize`; touched auth/secrets/broker credentials → `security-review`; done → `verify`. **A check at task start does not discharge a later trigger.** A parent skill (`t4-dev-workflow`) does not discharge its leaves (`tdd`, `verify`, …).

Coding behaviour follows `karpathy-guidelines`: simplest thing that works, surgical diffs, verifiable success criteria.

Delegation: `clink` is not configured on this machine, so `clink-subagents` / `clink-masteragent` are **not wired** (recorded 2026-11-27).

Companion ecosystems (recorded 2026-11-27):
- **superpowers** — installed.
- **9arm** — present (`debug-mantra`, `scrutinize`, `post-mortem`); `qwen-agent` not installed.
- **matt pocock** — installed; `setup-matt-pocock-skills` ran 2026-11-27 (see "Agent skills" below).

### Review gates — OpenCode (owner decision 2026-11-28, #149)

This subsection overrides the "before merge → `code-review` + `scrutinize`" trigger above, and any wording elsewhere that calls the OpenCode review advisory only. Full procedure, markers and the loop: `docs/agents/workflow.md` → "Review gates — OpenCode merge gate".

- **Every PR is reviewed by OpenCode** (the `review` check, bot `opencode-agent[bot]`). That review satisfies the before-merge `code-review` + `scrutinize` trigger. The agent does **not** spawn subagents to review its own work, except in the two cases below.
- **Risk paths also need one independent subagent review.** These are the `riskPaths` globs in `.claude/t4.json`: money/risk modules, secrets/config/auth, `.github/workflows/`, `.github/actions/`, `.claude/`, `.githooks/`. Record it with a PR comment whose first line is `<!-- t4:independent-review head=<sha> -->`.
- **Fallback if OpenCode is down** (model 429, App token failure, timeout, review missing for an infra reason): self-review with subagents as before (`code-review`, `scrutinize`, `simplify`, plus `security-review` when relevant). Record it explicitly with a comment whose first line is `<!-- t4:opencode-fallback head=<sha> reason="<cause>" -->`, plus the independent-review record. Never fall back silently. Dependabot PRs get no OpenCode review, so they take this path too.
- **Merge command:** `gh pr merge <n> --squash --match-head-commit <head sha>`, as its own plain command. Anything else that looks like a merge is refused.
- **Review → fix loop.** Push, and OpenCode reviews the new head. Fix each finding, or reply with the reason, then resolve the thread. Push again. Merge only when the latest review at the current head ends `Verdict: 0 blocker, 0 major`, and every bot thread from any round is resolved and either answered or outdated. If OpenCode has reviewed more than `opencodeMaxRounds` commits (default 5) and the gate still fails, **stop and ask the owner** instead of pushing again. Also stop and ask the owner when the same finding comes back a third time. The hook does not detect repeats, so this rule is the agent's own procedure.
- **The `review` check is not a required check**, so the fallback stays reachable.
- **Transition:** PR #144 (the OpenCode workflows) is on `main` as of e885f0e, so OpenCode reviews every PR now. The hook enforces this policy only once PR #150 is merged and `requireOpenCodeReview` is set to `true`. Until then, the agent follows the policy by procedure.
- **Still the agent's own work:** TDD, `verify`, and the in-session `simplify` / `security-review` triggers.
- **`T4-Gates` trailer:** `code-review=ran` (and `scrutinize=ran`) means OpenCode reviewed the work, or the fallback subagent review ran. The trailer is written before OpenCode has run, so `not-run` is the honest value then. The merge gate enforces the review, not the trailer.

## Session-start read protocol

1. `karpathy-guidelines` — load once.
2. `Obsidian-OrionFX/Home.md` — skim; open only the notes the task touches.
3. `docs/OPEN-WORK-LEDGER.md` — current open work; 🔴 rows have no issue.
4. The GitHub issue you're picking up — `gh issue view <n> --comments`.
5. `DONE.md` / `docs/reports/survey-manifest/` — only if the task needs history.

**Session end:** report each skill rule that did not hold as a `skill-feedback` issue on `xenodeve/xeno-skills` (search first, including closed; comment rather than duplicate; always pass `--repo`).

## Repo layout (today)

```
CLAUDE.md  CONTEXT.md  UBIQUITOUS_LANGUAGE.md  PRODUCT.md  DESIGN.md  DONE.md
docs/plans/master_planV1.1.md     requirements source
docs/OPEN-WORK-LEDGER.md        open work
docs/agents/                    domain.md, workflow.md, issue-tracker.md, triage-labels.md
docs/prd/  docs/contracts/  docs/catalog/   PRD-08/PRD-X01, generated JSON Schemas, CAT coverage matrix
backend/app/  migrations/  scripts/  tests/   Python backend (M02–M08, X01, X02, runtime loop)
frontend/                       M01 Web Dashboard (Bun, React/TS)
docs/adr/  docs/reports/  docs/research/  docs/superpowers/{plans,specs}/
Obsidian-OrionFX/               team memory vault
.claude/                        t4.json marker, hooks, settings
.githooks/                      pre-push guards
.github/workflows/t4-verify.yml CI: guards + lint/typecheck/test + frontend
```

Code layout follows master plan §7.1; `mt5/` (MQL5) is still planned (blocked on #17). Don't create empty service folders ahead of work.

## Commands

Backend (Python 3.13, `uv`): `uv sync` · `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` · `uv run pytest`. These four are `.claude/t4.json` `"verify"` and the CI `lint`/`typecheck`/`test` jobs. After a contract change in `backend/app/core/`: `uv run python scripts/gen_schemas.py` (a test fails if `docs/contracts/` is stale). Run the API locally: `uv run python -m app.api set-password` once, then `uv run python -m app.api serve` (binds the configured host, 127.0.0.1 by default).

Frontend (`frontend/`, Bun): `bun install` · `bun run lint` · `bun run typecheck` · `bun run test` · `bun run build`; `bun run dev` serves on :5173 and proxies `/api` and `/ws` to the API on :8000. These four run in the CI `frontend` job (Bun pinned to 1.4.5, since #80); local `.claude/t4.json` `"verify"` still covers only the backend.

Tooling notes (Windows, checked 2026-11-27): in git-bash `gh` resolves to `/c/Program Files/GitHub CLI/gh` and `python -c` runs; `bun` is at `~/.bun/bin/bun`. Bun for the frontend; `uv` + ruff + mypy (strict) + pytest for the backend.

Enable the git guards once per clone: `git config core.hooksPath .githooks`.

## Enforcement — what is actually in place

- **Claude hooks** (`.claude/settings.json`, marker `.claude/t4.json`): session-start injects `.claude/hooks/t4-directive.md` (a short directive to route through `using-t4`; the plugin, if installed, supplies the same); per-turn reminder; `PreToolUse` gate **blocks `gh pr create` with no referenced issue and dangerous git** (`reset --hard`, force-push, `clean -f`, `branch -D`). Binds Claude only.
- **git pre-push** (`.githooks/`): issue reference, tree budget, `T4-Gates:` trailer. Binds anyone who enabled `core.hooksPath`.
- **CI** (`t4-verify.yml`): `guards`, `lint`, `typecheck`, `test`, `frontend`; plus the OpenCode workflows (below). **Required checks since 2026-11-28:** the repo is public, and the ruleset `main-protection` (id 28272751) is active on the default branch with no bypass actors. It requires a PR and these five checks, and it blocks deletion and force-push. So a direct push to `main` is refused server-side, and a web merge needs green checks. The OpenCode `review` check is deliberately **not** required, because the fallback must stay reachable when OpenCode is down. `requireGreenCI: true` also makes the agent's `gh pr merge` wait for every check, including the non-required ones.
- **OpenCode (read-only; full table in `docs/agents/workflow.md` → OpenCode):** five workflows share `.github/actions/opencode-run`:
  - `opencode-review` (every collaborator PR) posts ONE `COMMENT` review plus inline comments, using the repo skills in `.opencode/skills/` (the review half of `t4-dev-workflow`; conditional skills are listed as skipped with a reason). The last line is `Verdict: <n> blocker, <n> major`, which the merge gate parses;
  - `opencode-ask` handles `/oc <question>` and `/opencode <question>` comments by collaborators, answer-only;
  - `opencode-audit` runs weekly on `main` and opens one bilingual `needs-triage`+`repo-ops` issue, which enters normal triage;
  - `opencode-triage` labels new trusted issues and asks for a missing EN/TH part;
  - `opencode-dispatch` runs an ad-hoc read-only prompt.

  **OpenCode never changes code.** No job has `contents: write`. The model runs with a deny-by-default tool policy: read, glob, grep and list in the checkout, plus the skill tool. It has no shell, no edit, no web, and no GitHub token. Trusted steps do all posting, as the OpenCode GitHub App (`opencode-agent[bot]`). Each step mints a short-lived App installation token by OIDC and revokes it after posting; the job `GITHUB_TOKEN` is read-only, except `issues: write` on the review job for the unavailable notice. On PR events, every token-holding script (shared action, `app-token.sh`, `post.py`) runs from the PR's **base** revision, never the head, except in the bootstrap PR that introduced the tooling (#144; the fallback is removed in a follow-up, see the ledger). The App installation itself has `contents: write` on all org repos. That is broader than needed; narrowing the repo selection is an owner step in the ledger. `/oc` refuses change requests and says Claude is the developer.

  **Review policy:** see "Review gates — OpenCode" above. The OpenCode review replaces the subagent self-review, and it is not advisory.

  **Model auth uses no repo secret.** Two OpenCode Console OIDC service accounts were created on 2026-11-28. Both are pinned to repo ID 1473623856 and limited to Models: Request:
  - "OrionFX PR Review" runs from pull requests;
  - "OrionFX Automation" runs from `main` only.

  Their client IDs live in repo variables `OPENCODE_PR_CLIENT_ID` and `OPENCODE_AUTOMATION_CLIENT_ID`. They are credential IDs, not secrets, and are kept out of the code because secret scanning misreads them as payment keys.

  **Free models are refused from CI**, so billing goes to the owner's Go plan. The model is `opencode-go/deepseek-v4.1-flash`, set in one place (the action's `model` default). Every job has a hard timeout.

  **When OpenCode is unavailable** (429, sign-in or App-token failure, timeout), the `review` check still finishes green. It posts `OpenCode unavailable: <cause>` as an issue comment, never as a review. The check goes red only on config or security causes. The `review` check will not be a required check: the merge-gate hook (#150) is the gate.

  **Limits:** the OpenCode checks are not required checks; a skipped check means no review ran; fork PRs are skipped; the repo is public, so thread text can bias answers but cannot add tools; jobs on `main` events work only after merge.
- Local `verify` is **armed** (ruff + format + mypy + pytest; backend only); `t4-gate` runs it before `gh pr merge`.
- `.claude/hooks/` holds only the 16 files the wired hooks reach (dependency closure checked 2026-11-27); the unwired `clink`/canary/debt/receipt scripts and the unread `using-t4.snapshot.md` were left out. The scripts are vendored from `xeno-skills` — fix bugs upstream, not here.
- **Known upstream defects (vendored, not patched here):** `.githooks/check-gate-ledger` reads the **oldest** `T4-Gates:` trailer on the branch (`git log … | tail -n 1`), so put the trailer on the first commit correctly; `t4-gate`'s `mcp__github__merge_pull_request` path skips the `requireGreenCI` check (no GitHub MCP server is configured here, so the path is unreachable today); `gh pr merge --squash <n>` (flag before the number) is not parsed as PR `<n>` — write `gh pr merge <n> --squash`.
- **Secret scanning + push protection: ON** (the repo is public; the owner enabled both on 2026-11-28). Push protection blocks known secret patterns server-side; still keep secrets in `.env` (gitignored) and run `/security-review` on every boundary change. Dependabot alerts + security-fix PRs are on.
- **OpenCode merge gate (#149; repo-local, not vendored):** `.claude/hooks/orionfx-opencode-merge-gate`, a second `PreToolUse` hook beside `t4-gate`. It is a 17th file in `.claude/hooks/` and is edited here, not upstream. When `"requireOpenCodeReview": true` is set in the working-tree `.claude/t4.json` **or** in the default branch's copy (read with `gh api`; its settings win), it gates Bash, PowerShell and Monitor commands and GitHub MCP merge tools. So a local edit or an old worktree cannot switch it off, though a local edit to `.claude/settings.json` that removes the hook still can. It blocks by printing a PreToolUse `deny` JSON on stdout and exiting 0, never by exit 2: on Windows the vendored `run-hook.cmd` loses the exit code (`exit /b %ERRORLEVEL%` inside a `( … )` block expands to 0). It accepts only `gh pr merge <n> … --match-head-commit <head>`, with known flags, no `--auto`/`--admin`, and no unquoted shell expansion. It refuses any other merge-looking text, including quoted or escaped keywords and heredoc bodies fed to an interpreter. Even that form is blocked unless one of these holds:
  - the OpenCode review at the head reports `Verdict: 0 blocker, 0 major`, and every bot thread from any round is handled;
  - the explicit fallback is recorded.

  On risk paths, the independent-review record is also required. Past `opencodeMaxRounds` reviewed commits, a failing gate says stop and ask the owner. The "same finding 3 times" stop rule is the agent's procedure; the hook does not detect repeats. It fails closed, with the deny JSON, on `gh` or API errors, runtime errors, unexpected exits and signals (a trap). Timeouts are left to `onFailure: "block"`, which needs Claude Code ≥ 2.1.295. `git push` to `main` is left to the GitHub ruleset. PR #144 (the OpenCode workflows) is on `main` (e885f0e). The gate is still committed as `false` in PR #150, and it is switched to `true` in a follow-up after #150 merges. Tests: `bash tests/hooks/test-orionfx-opencode-merge-gate.sh`, also run by the CI `guards` job. Limits:
  - Binds Claude only. A web merge is held only by the ruleset's required checks, not by the review gate.
  - A "trusted" thread reply or marker comes from an `OWNER`/`MEMBER`/`COLLABORATOR` account. The agent posts as the OWNER account, so this is identity-based, not proof that a human wrote it.
  - A text hook cannot close indirect forms: `$(…)`, a variable holding the command, encoded commands, a script written in one call and run in another. The ruleset is the server-side backstop.
  - `run-hook.cmd` exits 0 when it finds no bash (an upstream defect, fail-open). That is one more reason the gate relies on the deny JSON plus the ruleset, not on the exit code.

## Training-loop research autonomy (owner grant, 2026-11-28)

The owner has authorised the agent to **research independently inside the M04 training loop** (PRD #120, ADR 0001): to improve models and the training process itself, from the data the system collects, without asking first. This covers:
- features and transforms;
- labels;
- model families and hyperparameters;
- calibration;
- regime rules;
- sample weighting;
- validation tooling;
- data sources the plan already lists;
- the training pipeline's own code.

The freedom stops at these limits, which are not optional. The mechanism in full is `docs/agents/training-loop.md`.
- **Protocol first.** Exploration runs on development data: the walk-forward folds and the debug window. A result becomes a claim only under a pre-registered, hashed protocol version (PRD #120 slice 5). Never tune on the final holdout or the unread slice. An opened holdout becomes development data, so the next claim needs fresh forward data. Report best-of-N with N, and keep every run as evidence, including failures.
- **The goalposts are the owner's.** Targets, readiness thresholds and the holdout window change only when the owner certifies a new policy version. Research may *propose* a new version, with its evidence. It never adopts one.
- **Promotion follows ADR 0001.** Autonomous promotion and rollback are allowed in Shadow/Demo only, under a certified policy. Real-money Live stays owner-only.
- **Generated code is untrusted** (master plan §15.5 item 6). It goes through PR review, the T4 gates and tests. It runs in a training environment such as Colab with no broker, LLM or GitHub credentials and no production write access. Model weights load only through the trusted adapter, never by pickle.
- **Honesty.** Write findings up in `docs/research/`: what was tried, what failed, and who verified it. Never present a backtest as a real-market result or an offline metric as profit.

## Developer notification

On long/AFK work, notify when a task completes, when a confirm is needed (before closing issues / merging), or when an AFK batch is done — not for routine progress.

## Writing conventions

- **Chat, reports, status updates: Thai** (`t4-bro` register). Identifiers stay English and byte-exact.
- **GitHub tracker bodies (issues, PRDs, PR descriptions): bilingual** — English, then a Thai mirror of the same depth; titles English, conventional-commit style. Thai never summarizes.
- **Root governance docs** (`CONTEXT.md`, `UBIQUITOUS_LANGUAGE.md`, `PRODUCT.md`, `DESIGN.md`): `<!-- lang:en -->` / `<!-- lang:th -->` blocks, full mirror. **Exceptions, English only:** `CLAUDE.md` (agent operating manual), `DONE.md` and `docs/OPEN-WORK-LEDGER.md` (agent memory), `docs/agents/*`.
- **Code, commit messages, comments: English.**
- Domain terms come from `UBIQUITOUS_LANGUAGE.md` exactly; drifting to an "alias to avoid" is a defect.

## Agent skills

### Issue tracker

GitHub Issues on `Slow-Inc/OrionFX` via `gh`; bodies bilingual EN + TH. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles with default names, plus T4 Component/Type/Severity/Lifecycle groups. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context; the glossary is `UBIQUITOUS_LANGUAGE.md` (read it wherever a skill says `GLOSSARY.md`). See `docs/agents/domain.md`.

## Pointers

- `docs/agents/workflow.md` — grill → PRD → issues → TDD; auto-triggered skills
- `docs/agents/training-loop.md` — the M04 training loop: data → labels → training → evaluation tiers → claim protocol → autonomous Shadow/Demo promotion and rollback; built vs planned
- `docs/agents/domain.md` — how to read CONTEXT / glossary / ADRs / master plan
- `docs/adr/README.md` — decisions; `docs/reports/README.md` — records
