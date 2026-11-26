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
docs/adr/  docs/reports/  docs/research/  docs/superpowers/{plans,specs}/
Obsidian-OrionFX/               team memory vault
.claude/                        t4.json marker, hooks, settings
.githooks/                      pre-push guards
.github/workflows/t4-verify.yml CI: guards + lint/typecheck/test
```

Planned code layout: master plan §7.1 (`backend/` Python/FastAPI, `frontend/` React/TS, `mt5/` MQL5). Don't create empty service folders ahead of work.

## Commands

Backend (Python 3.13, `uv`): `uv sync` · `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` · `uv run pytest`. These four are `.claude/t4.json` `"verify"` and the CI `lint`/`typecheck`/`test` jobs. After a contract change in `backend/app/core/`: `uv run python scripts/gen_schemas.py` (a test fails if `docs/contracts/` is stale). Run the API locally: `uv run python -m app.api set-password` once, then `uv run python -m app.api serve` (binds the configured host, 127.0.0.1 by default). Frontend commands land with M01.

Tooling notes (Windows, checked 2026-11-27): in git-bash `gh` resolves to `/c/Program Files/GitHub CLI/gh` and `python -c` runs; `bun` is at `~/.bun/bin/bun`. Bun for the frontend; `uv` + ruff + mypy (strict) + pytest for the backend.

Enable the git guards once per clone: `git config core.hooksPath .githooks`.

## Enforcement — what is actually in place

- **Claude hooks** (`.claude/settings.json`, marker `.claude/t4.json`): session-start injects `.claude/hooks/t4-directive.md` (a short directive to route through `using-t4`; the plugin, if installed, supplies the same); per-turn reminder; `PreToolUse` gate **blocks `gh pr create` with no referenced issue and dangerous git** (`reset --hard`, force-push, `clean -f`, `branch -D`). Binds Claude only.
- **git pre-push** (`.githooks/`): issue reference, tree budget, `T4-Gates:` trailer. Binds anyone who enabled `core.hooksPath`.
- **CI** (`t4-verify.yml`): `guards`, `lint`, `typecheck`, `test`. **Not required checks** — Slow-Inc is on the free plan and private repos can't enforce rulesets. `requireGreenCI: true` makes the agent's `gh pr merge` wait for green CI; **a human merging on the web is not blocked by anything.**
- Local `verify` is **armed** (ruff + format + mypy + pytest); `t4-gate` runs it before `gh pr merge`.
- `.claude/hooks/` holds only the 16 files the wired hooks reach (dependency closure checked 2026-11-27); the unwired `clink`/canary/debt/receipt scripts and the unread `using-t4.snapshot.md` were left out. The scripts are vendored from `xeno-skills` — fix bugs upstream, not here.
- **Known upstream defects (vendored, not patched here):** `.githooks/check-gate-ledger` reads the **oldest** `T4-Gates:` trailer on the branch (`git log … | tail -n 1`), so put the trailer on the first commit correctly; `t4-gate`'s `mcp__github__merge_pull_request` path skips the `requireGreenCI` check (no GitHub MCP server is configured here, so the path is unreachable today); `gh pr merge --squash <n>` (flag before the number) is not parsed as PR `<n>` — write `gh pr merge <n> --squash`.
- **Secret scanning / push protection: unavailable** (GitHub 422 on this private free-plan repo). Nothing server-side stops a committed secret — keep secrets in `.env` (gitignored) and run `/security-review` on every boundary change. Dependabot alerts + security-fix PRs are on.

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
- `docs/agents/domain.md` — how to read CONTEXT / glossary / ADRs / master plan
- `docs/adr/README.md` — decisions; `docs/reports/README.md` — records
