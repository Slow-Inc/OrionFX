# Open Work Ledger — consolidated single source (2026-11-27)

> **Why this file exists:** open work gets scattered across GitHub issues, ADRs, plans, and
> DONE.md. Agents read issues but often miss the MD. This ledger consolidates **everything
> still open** — GitHub-tracked **and** MD-only — into one place, deduped, with a phased plan.
> **Read this file at session start (it is linked from CLAUDE.md).** When you finish an item,
> update its row here AND its GitHub issue; when you discover new work, add a row here and
> (for anything non-trivial) file an issue so it doesn't vanish into MD again.

**Legend:** ✅ done, pending merge · 🟢 buildable now · 🟡 gated (needs merge / resource /
decision) · 🔴 **UNTRACKED** (MD-only, no GitHub issue — highest miss-risk)

---

## Track 0 — Repo operating layer (T4 bootstrap)

| Item | Status | Gate | Next action |
|---|---|---|---|
| T4 bootstrap: hooks, guards, CI `guards` job, memory, records, governance docs | ✅ | PR merge | Merge the bootstrap PR |
| `docs/agents/issue-tracker.md` + `docs/agents/triage-labels.md` + labels | ✅ | PR merge | Done 2026-11-27 via `/setup-matt-pocock-skills` + T4 delta |
| CI `lint`/`typecheck`/`test` (backend) + local `verify` | ✅ | — | #5. Frontend jobs land with M01 |
| Upstream defects in vendored hook/guard scripts (see `CLAUDE.md` → Enforcement) | 🔴 | Report to `xenodeve/xeno-skills` | Not patched locally, to keep the scripts in sync with upstream |
| Secret scanning + push protection | 🔴 | GitHub returned 422 "not available for this repository" (private, free plan) | No server-side secret gate exists; rely on `.gitignore` + `/security-review`. Revisit with the plan/visibility |
| Required status checks / ruleset on `main` | 🔴 | Slow-Inc is on the free plan (rulesets not enforceable on private repos) | `requireGreenCI: true` is the stopgap; revisit if the org plan or repo visibility changes |

## Track 2 — master plan V1.1 additions (§15–16)

| Item | Status | Gate | Next action |
|---|---|---|---|
| Adopt V1.1 in repo docs | ✅ | PR merge | #19 |
| M08 follow-up: catalog storage, trusted artifact loading, isolation, audit (UST-01,07–10) | 🟢 | — | #20 |
| X01 follow-up: strategy/technique contracts + consolidated CAT coverage matrix (UST-01–04,11; AT-37) | 🟢 | — | #21 |
| M06 V1.1 obligations (UST-03,04,06–09,11,13; CAT-R01-*, CAT-X02-*) | 🟢 | — | folded into PRD-06 #12 |
| ADV catalog rows (P6–P8) | 🟡 | Phase P6+ | Tracked as `planned` in the coverage matrix (#21); not implemented in this run |

## Track 1 — P0: PRDs & contracts (master plan §10)

| Item | Status | Gate | Next action |
|---|---|---|---|
| 8 module PRDs (M01–M08) + PRD-X01 integration + PRD-X02 validation | 🟢 | — | Tracking issue #4; one sub-issue per PRD, filed as each starts. X01 ✅ #5 |
| Shared schemas: event envelope, money/time conventions, cycle/risk states | ✅ | — | #5 (PRD-X01). Broker adapter interface lands with M06 |
| Open decisions & safety TBDs | 🔴 | Owner decisions | `docs/plans/master_planV1.1.md` §13 |

---

## Management Plan — phased execution order

**Phase 0 — Unblock tracking.** ✅ Done 2026-11-27 — triage labels exist; merge PR #2.
**Phase 1 — Tracking hygiene.** File issues for the 🔴 rows in Track 1 (one per PRD, one for shared contracts).
**Phase 2 — P0 PRDs.** Contracts first (X01 + M06/M08), then module PRDs in parallel.
**Phase 3 — P1 Foundation.** First code lands → install the real CI jobs and arm `verify` in the same PR.

**Gating summary:** Phase 1 is now the multiplier — without an issue per PRD, no PRD work can pass `check-issue-ref`.
