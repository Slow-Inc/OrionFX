# Open Work Ledger — consolidated single source (2026-11-27)

> **Why this file exists:** open work gets scattered across GitHub issues, ADRs, plans, and
> DONE.md. Agents read issues but often miss the MD. This ledger consolidates **everything
> still open** — GitHub-tracked **and** MD-only — into one place, deduped, with a phased plan.
> **Read this file at session start (it is linked from CLAUDE.md).** When you finish an item,
> update its row here AND its GitHub issue; when you discover new work, add a row here and
> (for anything non-trivial) file an issue so it doesn't vanish into MD again.

**Legend:** ✅ done, pending merge · 🟢 buildable now · 🟡 gated (needs merge / resource /
decision) · ⛔ blocked (needs an external resource) · 🔴 **UNTRACKED** (MD-only, no GitHub issue — highest miss-risk)

---

## Track 0 — Repo operating layer (T4 bootstrap)

| Item | Status | Gate | Next action |
|---|---|---|---|
| T4 bootstrap: hooks, guards, CI `guards` job, memory, records, governance docs | ✅ | — | #1, merged via PR #2 |
| `docs/agents/issue-tracker.md` + `docs/agents/triage-labels.md` + labels | ✅ | — | Done 2026-11-27 via `/setup-matt-pocock-skills` + T4 delta |
| CI `lint`/`typecheck`/`test` (backend) + local `verify` | ✅ | — | #5 |
| CI `frontend` job (Bun 1.4.5: lint/typecheck/test/build) | ✅ | — | #79 / PR #80. Local `.claude/t4.json` `verify` still covers only the backend |
| Dependabot PRs pass the `guards` job (gate-ledger and issue-ref skipped only while Dependabot opened the PR and triggered the run) | ✅ | — | #93 / PR #94 (owner-approved); Dependabot PR #3 (`actions/checkout` v7) merged after it |
| OpenCode PR review (`opencode-review.yml`): owner steps | 🟡 | Owner | Install the OpenCode GitHub App (github.com/apps/opencode-agent) on `Slow-Inc/OrionFX`; it was not among the org's installations on 2026-11-28, and without it publishing the review fails closed. Optional: add the `OPENCODE_API_KEY` secret and set `model: opencode-go/<model>` to leave the free model. Issue: #143 |
| Upstream defects in vendored hook/guard scripts (see `CLAUDE.md` → Enforcement) | 🔴 | Report to `xenodeve/xeno-skills` | Not patched locally, to keep the scripts in sync with upstream |
| `.claude/t4.json` `autoMerge: true` (set 2026-11-27 at the developer's request for the AFK batch) | 🟡 | AFK batch end | Set back to `false` when the developer returns, unless they say to keep it |
| Secret scanning + push protection | 🔴 | GitHub returned 422 "not available for this repository" (private, free plan) | No server-side secret gate exists; rely on `.gitignore` + `/security-review`. Revisit with the plan/visibility |
| Required status checks / ruleset on `main` | 🔴 | Slow-Inc is on the free plan (rulesets not enforceable on private repos) | `requireGreenCI: true` is the stopgap; revisit if the org plan or repo visibility changes |

## Track 2 — master plan V1.1 additions (§15–16)

| Item | Status | Gate | Next action |
|---|---|---|---|
| Adopt V1.1 in repo docs | ✅ | — | #19 |
| M08 follow-up: catalog storage, trusted artifact loading, isolation, audit (UST-01,07–10) | ✅ | — | #20 — sandboxing beyond a time budget stays ADV (UST-07) |
| X01 follow-up: strategy/technique contracts + consolidated CAT coverage matrix (UST-01–04,11; AT-37) | ✅ | — | #21 — `docs/catalog/coverage.json`; update a row's state whenever its implementation lands |
| M06 V1.1 obligations (UST-03,04,06–09,11,13; CAT-R01-*, CAT-X02-*) | 🟡 | #121 | Folded into PRD-06 #12 (closed); MT5/EA parts wait on #17. **Corrected 2026-11-28:** the M06 veto never consults `ArtifactRegistry`, so UST-06/08/09 and AT-32 are not met. With 0 registry rows an intent is still allowed (BLK-04, reproduced). Tracked in #121 |
| ADV catalog rows (P6–P8) | 🟡 | Phase P6+ | Tracked as `planned` in the coverage matrix (#21); not implemented in this run |

## Track 1 — P0: PRDs & contracts (master plan §10)

| Item | Status | Gate | Next action |
|---|---|---|---|
| 8 module PRDs (M01–M08) + PRD-X01 integration + PRD-X02 validation | ✅ | — | All closed: M01 #72, M02 #28, M03 #37, M04 #45, M05 #51, M06 #12, M07 #58, M08 #7, X01 #5 (+ X01b #81, X01c #88), X02 #65. Umbrella #4 stays open for the blocked slices below |
| Shared schemas: event envelope, money/time conventions, cycle/risk states | ✅ | — | #5 (PRD-X01). Broker adapter interface lands with M06 |
| M08 System Core (PRD #7): storage/journal/config/lease/backup (#8), API/auth/WS (#9) | ✅ | — | Done |
| M06 Risk & Execution (PRD #12): broker port + fake broker (#13), risk veto + DD (#14), cycle/target/trailing/close (#15), engine loop (#16) | ✅ | — | Done against the fake broker |
| M06 slice E: real MT5 adapter + MQL5 Execution EA, broker-side Emergency SL (AT-09, AT-10) | ⛔ | Needs an MT5 terminal + demo account | #17 — not done; engine runs only against the fake broker |
| M03 live LLM provider (PRD #98): reasoning prompt v2 (#99 ✅), OpenAI-compatible adapter for OpenCode Go (#100 ✅), off-tick news worker (#101 ✅) | ✅ | — | Done, not yet run in `serve` (needs a news source and #17); nothing journals forecasts or feature snapshots at runtime yet, so worker reasoning sees news only; benchmark + model choice: `docs/research/2026-11-27-m03-llm-provider-opencode-go.md` (#97): `qwen3.8-flash` primary, `deepseek-v4.1-flash` fallback |
| PRD #98 stories not met: the backend does not load `.env` (`resolve_secret` reads the process environment, so `OPENCODE_API_KEY` must be exported); no Go usage-meter read for health; per-call `allowance` is not journaled | 🔴 | — | Load `.env` with the `serve` wiring (#17 / news source); journal allowance and poll `/v1/usage` from the news worker. Noted on #98 |
| M07 training dataset from real operation (PRD #107): per-bar snapshots + outcome labels (#108 ✅), recorded predictions kept out of trading (#109 ✅), deterministic leak-free export (#110 ✅) | ✅ | — | Done. Collect now, train later (#48 out of scope). The recorder is not yet run in `serve` (#17); model cards need a training cutoff before #48 so back-filled predictions can be judged in- or out-of-sample |
| M03 adapter hardening: `api_key_ref` can name any `env:` secret and `base_url` any https host (owner-only) | 🔴 | Owner decision | Found in #100 security review (needs the owner's own session, so not a vulnerability); optional fix: allowlist key names such as `env:LLM_*` |
| M03 news half: licensed news/calendar source | ⛔ | OD-06 licensed source | #41 — not done; M03 news runs on fakes |
| M05 news veto (PRD #114): live, cited news with net impact >= 0.6 against the chart side turns an entry into WAIT `NEWS_VETO`; it never opens, adds or flips (#115 ✅) | ✅ | — | Outcome report done (#116: `veto_report` saved / missed / flat / unknown). Next: tune `NEWS_VETO_IMPACT` on Shadow/Demo data once the bot runs (#17); two-model agreement is a later option |
| Real XAUUSD sessions break the data path: technique windows treat the daily break and the weekend as gaps, so H1 RSI/ATR/ADX/EMA/MACD are usable on 0% of bars and snapshot quality is OK on 0%; a forecast inherits DEGRADED, so EV is unknown on 100% of decisions (BLK-01/01b). M15/H1 labels recorded on M5 land on about 1/3 and 1/12 of rows (MAJ-03). Fusion picks the alphabetically first proposal (MAJ-04.3). `load_model` is not point-in-time (MAJ-09.1). Readiness unions subjects (BLK-05). All verified 2026-11-28 | 🔴 | PRD #120 Slice 0 | Blocks the training loop and any replay or backtest verdict. Strategy and technique rows marked `tested` were tested only on 24/7 bars |
| Session follow-ups from #124: market holidays and early closes have no calendar, so they are gaps and block long windows for up to 139 bars (fail-closed); no production caller passes a session yet (`XAUUSD_NY_1700` is [PROPOSED] and must be confirmed per source with `audit`); bars already stored with a GAP flag after a break still block windows; labels and `horizon_end` count wall-clock time across a break | 🟡 | #125, #127, #128 | Wire the schedule per data source in #128 (checked with `audit`); #127 asserts that the session is actually passed through; #125 handles labels across a break; add a holiday calendar later |
| #125 follow-ups: (a) outcome `spread` changed meaning in #125. It is now the max spread of the measuring bar (usually M5), not the horizon bar, and rows labelled earlier in the same `dataset_version` still hold the old value; the triple-barrier labels (#129) must define spread themselves. (b) `ExperienceLog` has no unlabellable mark, so a decision whose end bar is missing is re-queried every pass (bounded by the decision count, primary-key lookups). (c) Unlabellable marks are final, so a replay that later fills a hole does not relabel the row. | 🟡 | #129 / M07 slice | Track; no impact until training (#130) |
| #126 follow-ups: (a) `LoadedPredictor` can be forged in process with `dataclasses.replace` (mode, `approved_until`), as it already could before #126. This is not an external boundary, and the accepted limitation stands until predictors are tracked by identity. (b) Point-in-time eligibility refuses replay of a model approved after the replay window starts (fail-closed). Historical evaluation of a new model must use `load_for_research` and the PRD #120 protocol, not the trading loader | 🟡 | #130 / M08 slice | Track |
| Pre-Demo hardening from the verified pipeline review (Emergency/DD scope, Emergency button lock, Manual Position adoption on reconcile, Demo/Live and symbol checks, registry eligibility, reconcile while disconnected, and others) | ⛔ | Before Demo on MT5 (#17) | #121 holds the verified table, with each row marked reproduced or traced, plus the owner decisions |
| M04 training loop + system backtest (PRD #120) | 🟡 | Slice 0 first; real training needs owner sign-off on targets, holdout and readiness thresholds | Pre-registered protocol, bounded budget, a holdout opened once, anti-overfitting rules adopted from mip-research; runs on Colab CPU high-mem |
| M04 slice C: trained XGBoost/LightGBM artifacts + adapters | ⛔ | Needs training data and trained models | #48 — not done; M04 has the baseline and ensemble only. Plan: `docs/research/2026-11-28-m04-dataset-training-plan.md`. Steps 1–4 can start now; the holdout and every `[PROPOSED]` number need owner sign-off |
| Open decisions & safety TBDs | 🔴 | Owner decisions | `docs/plans/master_planV1.1.md` §13 |
| Same-lot recovery ADD on a losing cycle in Live: only M05 refuses it (M06 veto refuses size-ups only) | 🔴 | Owner decision (RSK-06 scope) | Decide whether M06 should also deny it; then a veto rule + test (found in #54 review) |
| Local `verify` for `frontend/` (`.claude/t4.json`) | 🟡 | Owner review: `.claude/t4.json` is an enforcement path | CI `frontend` job landed in #80; locally run `cd frontend && bun run lint && bun run typecheck && bun run test && bun run build` |
| Positions read model for the dashboard | ✅ | — | #82: `positions.snapshot` (bot only) → `/api/positions` → Trading view |
| `/api/account` and `/api/positions` read the newest snapshot regardless of account/bot id | 🔴 | M08 slice | Filter by the configured account/bot once multiple logins are supported (found in #82 review; pre-existing) |
| Runtime decision loop: M02 → evidence → M05 decide/manage → journal → M06, plus M07 experience and labels each tick | ✅ | — | #84: `app.runtime.loop.DecisionLoop`, tested against replayed bars and the real M06 engine on the fake broker |
| Run the decision loop in the `serve` process with real providers (MT5 quote/candle feed, M04 forecasts, M03 news/reasoning) | ⛔ | Blocked | Needs the MT5 adapter (#17), a licensed news source (#41) and trained models (#48). The LLM side is ready: OpenCode Go key, adapter (#100) and an off-tick news worker with `journal_evidence` (#101) |
| M06 exits (CLOSE/CLOSE_ALL) ignore `intent.cycle_id`; a stale exit from cycle A could close cycle B's basket | ✅ | — | #89: exit must name the open cycle and closes only that cycle's positions |
| Bot Positions left from a closed cycle while the next cycle is open: the loop refuses to decide around them | 🔴 | #121 (before Demo) | **Corrected 2026-11-28:** the earlier claim that Emergency Stop or DD breach closes them is false. Both close only `open_cycle_id()` positions and report `COMPLETED` while leftovers stay open (BLK-02, reproduced). The fix is in #121 |
| M06 state read from the journal is "latest by seq", not as-of `now`; replay on a reused database can see the future | 🔴 | X02/M08 slice | Replay on a fresh database, or read state as-of the replay clock (found in #84 scrutiny) |
| The loop recomputes the experience's direction and horizon instead of `Decision` carrying what it acted on | ✅ | — | #90: `Decision.direction`/`horizon` set once in fusion; EXIT bets against the held side |
| No expression index for `intent_id` lookups on `risk.decision` | 🔴 | M08 slice | Add an index if journal growth makes `find`/`unanswered` slow (found in #84 review) |
| `available_at` on `LlmReasoning` (no look-ahead guard for reasoning) | ✅ | — | #83: required, stamped after the answer returns, judged by `live()` in fusion |
| `StrategyProposal` has no availability time; fusion checks only its expiry | 🔴 | Contract change (X01) | Add `available_at` if proposals are ever built ahead of `now` (found in #83 review; pre-existing) |

---

## Management Plan — phased execution order

**Phase 0 — Unblock tracking.** ✅ Done 2026-11-27 — triage labels exist; PR #2 merged.
**Phase 1 — Tracking hygiene.** ✅ Done — one issue per PRD and slice (see Track 1).
**Phase 2 — P0 PRDs.** ✅ Done — every module PRD plus X01/X02 is closed.
**Phase 3 — P1 Foundation.** ✅ Done — backend CI jobs + local `verify` (#5), frontend CI job (#80).

**Gating summary:** what remains is blocked on external resources — MT5 + demo account (#17), a licensed news source (#41; the LLM side is done), trained models (#48) — plus the 🔴 owner-decision rows above.
