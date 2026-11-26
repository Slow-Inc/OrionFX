# CLAUDE.md — OrionFX

Agent operating manual. OrionFX is an **agent-primary** T4 repo (Slow-Inc): the coding agent is the main developer, so these docs are its working memory.

## Engineering north-star

A personal XAUUSD AI auto-trading bot on MT5 where **risk is independent of AI**: AI proposes, the Risk & Execution Engine (M06) decides, and the MQL5 Execution EA is the only trade writer. Safety and refusal-when-uncertain outrank profit and availability. Requirements of record: `docs/plans/master_planV1.md` — [LOCKED] items change only through a Change Request (§13.1).

**Never:** use real money, real credentials, or real account data during build/test; let an LLM send broker orders; touch **Manual Positions**; claim guaranteed profit or exact stops.

## Standing defaults

**`using-t4` is the standing default for every task in this repo — not a pointer read once.** Route through it before acting, and **re-route at every phase boundary**: after writing code → `simplify`; before merge → `code-review` + `scrutinize`; touched auth/secrets/broker credentials → `security-review`; done → `verify`. **A check at task start does not discharge a later trigger.** A parent skill (`t4-dev-workflow`) does not discharge its leaves (`tdd`, `verify`, …).

Coding behaviour follows `karpathy-guidelines`: simplest thing that works, surgical diffs, verifiable success criteria.

Delegation: `clink` is not configured on this machine, so `clink-subagents` / `clink-masteragent` are **not wired** (recorded 2026-11-27).

Companion ecosystems (recorded 2026-11-27):
- **superpowers** — installed.
- **9arm** — present (`debug-mantra`, `scrutinize`, `post-mortem`); `qwen-agent` not installed.
- **matt pocock** — `grilling`, `to-prd`, `to-issues`, `tdd`, `triage` present; `setup-matt-pocock-skills` **pending install by the developer** (blocks `docs/agents/issue-tracker.md` + `triage-labels.md`).

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
docs/plans/master_planV1.md     requirements source
docs/OPEN-WORK-LEDGER.md        open work
docs/agents/                    domain.md, workflow.md (issue-tracker.md, triage-labels.md pending)
docs/adr/  docs/reports/  docs/research/  docs/superpowers/{plans,specs}/
Obsidian-OrionFX/               team memory vault
.claude/                        t4.json marker, hooks, settings
.githooks/                      pre-push guards
.github/workflows/t4-verify.yml CI (guards only until P1)
```

Planned code layout: master plan §7.1 (`backend/` Python/FastAPI, `frontend/` React/TS, `mt5/` MQL5). Don't create empty service folders ahead of work.

## Commands

No code yet (phase P0). When P1 Foundation lands, record here: backend lint/typecheck/test, frontend lint/typecheck/test/build, and set the fast subset as `.claude/t4.json` `"verify"` + matching CI jobs.

Tooling notes (Windows): `gh` → `"/c/Program Files/GitHub CLI/gh.exe"` if not on PATH; `bun` → `~/.bun/bin/bun`. Bun for the frontend; Python tooling for the backend is decided in P1 (master plan §5.2 is [PROPOSED]).

Enable the git guards once per clone: `git config core.hooksPath .githooks`.

## Enforcement — what is actually in place

- **Claude hooks** (`.claude/settings.json`, marker `.claude/t4.json`): session-start injects `using-t4`; per-turn reminder; `PreToolUse` gate **blocks `gh pr create` with no referenced issue and dangerous git** (`reset --hard`, force-push, `clean -f`, `branch -D`). Binds Claude only.
- **git pre-push** (`.githooks/`): issue reference, tree budget, `T4-Gates:` trailer. Binds anyone who enabled `core.hooksPath`.
- **CI** (`t4-verify.yml`): `guards` job only. **Not a required check** — Slow-Inc is on the free plan and private repos can't enforce rulesets. `requireGreenCI: true` makes the agent's `gh pr merge` wait for green CI; **a human merging on the web is not blocked by anything.**
- Local `verify` is **off** (`""`) until there is code to test.
- **Secret scanning / push protection: unavailable** (GitHub 422 on this private free-plan repo). Nothing server-side stops a committed secret — keep secrets in `.env` (gitignored) and run `/security-review` on every boundary change. Dependabot alerts + security-fix PRs are on.

## Developer notification

On long/AFK work, notify when a task completes, when a confirm is needed (before closing issues / merging), or when an AFK batch is done — not for routine progress.

## Writing conventions

- **Chat, reports, status updates: Thai** (`t4-bro` register). Identifiers stay English and byte-exact.
- **GitHub tracker bodies (issues, PRDs, PR descriptions): bilingual** — English, then a Thai mirror of the same depth; titles English, conventional-commit style. Thai never summarizes.
- **Root governance docs:** `<!-- lang:en -->` / `<!-- lang:th -->` blocks, full mirror.
- **Code, commit messages, comments: English.**
- Domain terms come from `UBIQUITOUS_LANGUAGE.md` exactly; drifting to an "alias to avoid" is a defect.

## Pointers

- `docs/agents/workflow.md` — grill → PRD → issues → TDD; auto-triggered skills
- `docs/agents/domain.md` — how to read CONTEXT / glossary / ADRs / master plan
- `docs/adr/README.md` — decisions; `docs/reports/README.md` — records
