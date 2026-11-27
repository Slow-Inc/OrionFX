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
| `.claude/t4.json` `autoMerge: true` (set 2026-11-27 at the developer's request for the AFK batch) | 🟡 | AFK batch end | Set back to `false` when the developer returns, unless they say to keep it |
| Secret scanning + push protection | 🔴 | GitHub returned 422 "not available for this repository" (private, free plan) | No server-side secret gate exists; rely on `.gitignore` + `/security-review`. Revisit with the plan/visibility |
| Required status checks / ruleset on `main` | 🔴 | Slow-Inc is on the free plan (rulesets not enforceable on private repos) | `requireGreenCI: true` is the stopgap; revisit if the org plan or repo visibility changes |

## Track 2 — master plan V1.1 additions (§15–16)

| Item | Status | Gate | Next action |
|---|---|---|---|
| Adopt V1.1 in repo docs | ✅ | PR merge | #19 |
| M08 follow-up: catalog storage, trusted artifact loading, isolation, audit (UST-01,07–10) | ✅ | — | #20 — sandboxing beyond a time budget stays ADV (UST-07) |
| X01 follow-up: strategy/technique contracts + consolidated CAT coverage matrix (UST-01–04,11; AT-37) | ✅ | — | #21 — `docs/catalog/coverage.json`; update a row's state whenever its implementation lands |
| M06 V1.1 obligations (UST-03,04,06–09,11,13; CAT-R01-*, CAT-X02-*) | 🟢 | — | folded into PRD-06 #12 |
| ADV catalog rows (P6–P8) | 🟡 | Phase P6+ | Tracked as `planned` in the coverage matrix (#21); not implemented in this run |

## Track 1 — P0: PRDs & contracts (master plan §10)

| Item | Status | Gate | Next action |
|---|---|---|---|
| 8 module PRDs (M01–M08) + PRD-X01 integration + PRD-X02 validation | 🟢 | — | Tracking issue #4; one sub-issue per PRD, filed as each starts. X01 ✅ #5 |
| Shared schemas: event envelope, money/time conventions, cycle/risk states | ✅ | — | #5 (PRD-X01). Broker adapter interface lands with M06 |
| M08 System Core (PRD #7): storage/journal/config/lease/backup (#8), API/auth/WS (#9) | ✅ | — | Done |
| M06 Risk & Execution (PRD #12): broker port + fake broker (#13), risk veto + DD (#14), cycle/target/trailing/close (#15), engine loop (#16) | ✅ | — | Done against the fake broker |
| M06 slice E: real MT5 adapter + MQL5 Execution EA, broker-side Emergency SL (AT-09, AT-10) | 🟡 | Needs an MT5 terminal + demo account | #17 — not done; engine runs only against the fake broker |
| Open decisions & safety TBDs | 🔴 | Owner decisions | `docs/plans/master_planV1.1.md` §13 |
| Same-lot recovery ADD on a losing cycle in Live: only M05 refuses it (M06 veto refuses size-ups only) | 🔴 | Owner decision (RSK-06 scope) | Decide whether M06 should also deny it; then a veto rule + test (found in #54 review) |
| Frontend CI jobs (lint/typecheck/test/build) + `verify` for `frontend/` | 🟡 | Owner review: touches `.github/workflows` and `.claude/t4.json` (enforcement paths) | Separate PR after M01; run locally meanwhile (`cd frontend && bun run lint && bun run typecheck && bun run test && bun run build`) |
| Positions read model for the dashboard | ✅ | — | #82: `positions.snapshot` (bot only) → `/api/positions` → Trading view |
| `/api/account` and `/api/positions` read the newest snapshot regardless of account/bot id | 🔴 | M08 slice | Filter by the configured account/bot once multiple logins are supported (found in #82 review; pre-existing) |
| Runtime decision loop: M02 → evidence → M05 decide/manage → journal → M06, plus M07 experience and labels each tick | ✅ | — | #84: `app.runtime.loop.DecisionLoop`, tested against replayed bars and the real M06 engine on the fake broker |
| Run the decision loop in the `serve` process with real providers (MT5 quote/candle feed, M04 forecasts, M03 news/reasoning) | ⛔ | Blocked | Needs the MT5 adapter (#17), news/LLM keys (#41) and trained models (#48); the loop takes quote and evidence providers so they plug in without changes |
| M06 exits (CLOSE/CLOSE_ALL) ignore `intent.cycle_id`; a stale exit from cycle A could close cycle B's basket | ✅ | — | #89: exit must name the open cycle and closes only that cycle's positions |
| M06 state read from the journal is "latest by seq", not as-of `now`; replay on a reused database can see the future | 🔴 | X02/M08 slice | Replay on a fresh database, or read state as-of the replay clock (found in #84 scrutiny) |
| The loop recomputes the experience's direction and horizon instead of `Decision` carrying what it acted on | 🔴 | M05 slice | Put direction and horizon on `Decision` (found in #84 simplify) |
| No expression index for `intent_id` lookups on `risk.decision` | 🔴 | M08 slice | Add an index if journal growth makes `find`/`unanswered` slow (found in #84 review) |
| `available_at` on `LlmReasoning` (no look-ahead guard for reasoning) | ✅ | — | #83: required, stamped after the answer returns, judged by `live()` in fusion |
| `StrategyProposal` has no availability time; fusion checks only its expiry | 🔴 | Contract change (X01) | Add `available_at` if proposals are ever built ahead of `now` (found in #83 review; pre-existing) |

---

## Management Plan — phased execution order

**Phase 0 — Unblock tracking.** ✅ Done 2026-11-27 — triage labels exist; merge PR #2.
**Phase 1 — Tracking hygiene.** File issues for the 🔴 rows in Track 1 (one per PRD, one for shared contracts).
**Phase 2 — P0 PRDs.** Contracts first (X01 + M06/M08), then module PRDs in parallel.
**Phase 3 — P1 Foundation.** First code lands → install the real CI jobs and arm `verify` in the same PR.

**Gating summary:** Phase 1 is now the multiplier — without an issue per PRD, no PRD work can pass `check-issue-ref`.
