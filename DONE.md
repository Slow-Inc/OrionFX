# DONE — Agent Session Log

> Newest entry on top. One dated `##` heading per shipped unit so an agent can jump to one.
> When this crosses ~a few hundred lines or a phase closes, move older entries to
> `DONE-archive-<period>.md` and leave a redirect line here.

---

## T4 operating-layer bootstrap (2026-11-27, t4-project-bootstrap, branch `chore/1-t4-bootstrap`)

**Goal:** give OrionFX the T4 agent-primary operating layer at the Active tier before any PRD or code work starts, so later sessions keep context and meet the same gates.

**Shipped:**
- `.claude/t4.json`, `.claude/hooks/*`, `.claude/settings.json` — Claude hooks (session-start map, per-turn reminder, `PreToolUse` gate)
- `.githooks/*` — pre-push guards (issue ref, tree budget, gate ledger)
- `.github/workflows/t4-verify.yml` — `guards` job only (no code yet); `.github/dependabot.yml`
- `CLAUDE.md`, `CONTEXT.md`, `UBIQUITOUS_LANGUAGE.md`, `PRODUCT.md`, `DESIGN.md`
- `docs/OPEN-WORK-LEDGER.md`, `Obsidian-OrionFX/`, `docs/agents/{domain,workflow}.md`, `docs/adr/README.md`, `docs/reports/*`, `docs/research/README.md`, `docs/plans/README.md`, `docs/superpowers/{plans,specs}/README.md`

- `docs/agents/{issue-tracker,triage-labels}.md` + 26 labels created on `Slow-Inc/OrionFX` (via `/setup-matt-pocock-skills` + T4 delta)
- Review fixes after `/code-review` + `/scrutinize`: exec bits on hook/guard scripts, 8 unreachable hook files removed, CI checks out the PR branch by name and runs on PRs only, stale/inaccurate doc claims corrected

**Validation:** `run-hook.cmd t4-gate` denies `git reset --hard` when invoked both via bash and via `cmd.exe`; CI `guards` green on PR #2. Not run: a session on Linux/macOS.

**Next:** P0 PRD issues (ledger Track 1); upstream defects in the vendored scripts listed in `CLAUDE.md` → report to `xenodeve/xeno-skills`.

---
