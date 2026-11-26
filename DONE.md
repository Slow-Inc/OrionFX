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

**Validation:** see the bootstrap PR — hooks-layer presence check, placeholder grep, guard scripts run locally.

**Next:** issue-tracker + triage labels wait on `setup-matt-pocock-skills` (ledger Track 0).

---
