# DONE — Agent Session Log

> Newest entry on top. One dated `##` heading per shipped unit so an agent can jump to one.
> When this crosses ~a few hundred lines or a phase closes, move older entries to
> `DONE-archive-<period>.md` and leave a redirect line here.

---

## M02 slice B — indicator techniques CAT-T01 + tick volume CAT-T09-01 (2026-11-27, /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/30-m02-indicators`)

**Goal:** deterministic MVP indicators as `TechniqueObservation`s that never emit a false valid value (PRD-02 #28, slice #30; UST-02, UST-03).

**Shipped:** `backend/app/market_data/techniques/indicators.py` (SMA, EMA, WMA, RSI, MACD, ATR, ADX, DMI, Bollinger Bands, labelled tick volume; `observe`, `make_library`); `QualityFlag` (core/enums) and `TIMEFRAME_DURATION` (core/time) shared with the candle engine. Catalog: CAT-T01-01..09 and CAT-T09-01 → tested (replay data only).

**Validation:** `uv run pytest` → all pass (72 indicator tests; warm-up, gap, non-contiguous, expired run for every technique; values match an independent float implementation). 22 mutation probes caught (2 survivors fixed by richer test data: gap opens and outside bars).

**Process note (honest record):** in this slice the implementation was written before its tests (TDD order not kept). Compensated with an independent reference implementation and mutation probes on every formula and guard.

**Review fixes:** fixed lookback per technique (value depends only on the bars in `input_refs`), contiguity check, unknown volume is MISSING_INPUT not 0, an expired bar is STALE, `now` before availability is refused (look-ahead), normalized Bollinger id, evidence refs point at tests that run each row.

---

## M02 slice A — timestamps, symbol mapping, candles, data quality (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/29-m02-candles`)

**Goal:** one honest source of XAUUSD candles (PRD-02 #28, slice #29; DAT-01, DAT-04, UST-03).

**Shipped:** `backend/app/market_data/{port,candles}.py` (`MarketDataPort`, `ReplaySource`, `CandleEngine`, `ClosedBar`); `broker_to_utc`, `fresh`, `CLOCK_SKEW` in `core/time.py` (veto's `quote_fresh` now uses `fresh`); `CandleClosed.quality_flags`.

**Validation:** `uv run pytest` → 305 passed (18 market-data tests: DST, finality, M5/M15/H1 agreement, out-of-order, duplicate, crossed/NaN/zero quotes, future tick, gap in and after bars, staleness). 13 mutation probes caught. Review fixes: future ticks refused (a stale DST offset no longer freezes the feed), gap flags published and propagated to M15/H1, one symbol per engine, non-finite quotes invalid, same-timestamp duplicates, one count per outage.

**Not done:** MT5 read adapter (blocked like #17). Snapshot does not downgrade to DEGRADED on recent issues (issues are counted on the engine).

---

## M08 V1.1 — versioned registry, trusted artifact loading, isolated runs (2026-11-27, /tdd + /simplify + /code-review + /scrutinize + /security-review, branch `feat/20-m08-registry`)

**Goal:** registry storage and safe loading for strategies/techniques (#20; §15.5, UST-01, UST-07..10, CAT-X04-01).

**Shipped:** `migrations/0005_registry.sql` (immutable versions, append-only eligibility with FK, UPDATE/DELETE triggers); `backend/app/storage/registry.py` (`ArtifactRegistry`); `backend/app/storage/artifacts.py` (`ArtifactLoader.load(mode=)` / `load_for_research`, `run_isolated`); `SubjectKind`, `sha256_ref`, id/version may not contain '@'.

**Validation:** `uv run pytest` → 287 passed. 14 mutation probes (hash check, allowlist dot, registered check, child error, version immutability, eligibility hash, latest wins, eligibility gate, parent init, metadata compare, modes subset, expiry, …) each caught; one survivor showed a redundant check, removed. security-review: no finding ≥ 8 (nothing reachable from HTTP; re-run when a route or LLM path calls the loader/registry).

**Skipped (recorded):** moving the loader/runner into `decision/registry` and `learning/strategy_research` (those packages land with M05/M07); audit rows still use wall-clock time (same as config_store).

**Not done:** isolation is a time-budgeted process, not a security sandbox (UST-07 ADV); functions returned by `load` cannot be pickled into `run_isolated` (refused, not run unverified).

---

## X01 V1.1 — strategy/technique contracts + CAT coverage matrix (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/21-x01-catalog`)

**Goal:** the §15.5 shared contracts and one machine-readable disposition for all 201 catalog rows (#21; AT-37, §16.4).

**Shipped:** `StrategyManifest`, `TechniqueObservation`, `StrategyProposal`, `EligibilityRecord` (+ `CatalogId`, `Sha256Ref`) and events `technique.observed`, `strategy.proposed`, `strategy.candidate`, `strategy.eligibility.updated`; `backend/app/core/catalog.py` (`CoverageEntry`, `parse_plan_rows`, `coverage_summary`); `scripts/gen_catalog.py`; `docs/catalog/coverage.json`.

**Coverage at merge:** 201 rows — 160 planned, 28 blocked (licensed news/LLM keys, order-flow/DOM, real volume, related-instrument feeds, training), 11 excluded (X02 exclusions carry enforcement test ids), 1 tested (CAT-R01-05 Partial Close, fake broker only), 0 certified. CAT-R01-04 stays planned: its M06 half is tested, the adaptive M05 input is not built.

**Validation:** `uv run pytest` → 266 passed. 8 mutation probes on the matrix (drop a row, X planned, certified claim, tested without tests, fake test id, blocked without reason, owner drift, D row live) each turned a test red. Review fixes: row rules live in the model validator; dependency column compared; evidence refs must be real repo files and line-start test defs; eligibility records self-consistent (revoked/disabled ⇒ DISABLED, modes fit eligibility); proposals for entries need direction and lot; summary per phase/family/state + excluded. Noted, not changed: `ModelCandidate.artifact_sha256` keeps bare hex (predates `Sha256Ref`).

---

## M06 slice D — engine loop, control commands, restart reconcile (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/16-m06-engine`)

**Goal:** the M06 Risk & Execution loop that ties the broker port, risk veto, drawdown tracker and cycle book together (PRD-06 #12, slice #16).

**Shipped:** `backend/app/engine/engine.py` (`Engine.start()/tick()`, `EngineSettings`, `LeaseLost`); `CloseWorkflow.net_inputs/cycle_positions/cancel_bot_orders`; `quote_fresh` in `risk/veto.py`. One tick: lease → requires_reconcile → control commands → drawdown (cash-flow aware, cursor in the same txn) → cycle target/trailing/close → trade intents (veto + engine checks CYCLE_NOT_ACTIVE / CYCLE_MISMATCH / RECONCILE_REQUIRED / NOTHING_TO_CLOSE) → account snapshot. Pending Emergency Stop / Finish Cycle persisted; restart never auto-resumes.

**Validation:** `uv run pytest` → 243 passed (37 engine integration tests: AT-01..08, AT-11, AT-12, AT-13, AT-23, AT-32, AT-34, AT-42 against the fake broker); ruff + mypy clean. 20 mutation probes on the engine each turned a test red. Review fixes: exit sizing per target within the approved lot, keep going past a vanished position, NOTHING_TO_CLOSE, poison intent isolated (ENGINE_ERROR), ALLOW interrupted before dispatch reported not retried, cash-flow cursor atomic with DD update, Emergency Stop announced once / survives restart / survives a DD lock mid-close, Finish Cycle ACCEPTED then COMPLETED on stop, SAFE_MODE not kept without a handler, one deal read per Cycle Net, second protection pass only after an execution.

**Skipped (recorded):** moving engine entry checks into `RiskContext`; an index for `journal.find`; per-intent read reuse; a dedicated event for system lock notices (`control.completed` is kept because `/api/status` derives control state from it). AT-09/AT-10 and AT-41 composites are not covered here (#17 needs MT5; composites land with M05).

**Next:** #17 stays blocked (no MT5). Next module PRD.

---

## M06 slice C — cycle book, Profit Target, basket trailing, close workflow (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/15-m06-cycle`)

**Goal:** a Cycle that closes on its Profit Target or trailing floor, and only claims CLOSED when the broker agrees (PRD-06 #12, slice #15; PFT-01..05, AT-01..04, CAT-R01-04/05).

**Shipped:** `backend/app/profit/{cycle,close}.py`, `migrations/0004_cycles.sql`, trailing bounds in `RiskPolicy`, `OwnershipRegistry.positions_of_cycle`, shared `dec_or_none`/`text_or_none` in `app.storage.db`.

**Validation:** `uv run pytest` → 205 passed; ruff + mypy clean. Mutation probes (floor monotonic, reconcile-first, CLOSING latch, broker-state check, vanished position keeps the pass going, pending-order cancel, sticky order blocks CLOSED) each turned the matching test red. Review fixes: non-positive target refused (code + CHECK); activation capped at 0.9 x target so trailing cannot be switched off; a vanished position no longer stops the pass; bot pending orders cancelled and required gone before CLOSED; Deal commission/swap sign documented (adapter normalises MT5); one query per cycle's positions/deals. Skipped: grouping trailing fields into a sub-model (no behaviour change; revisit with M05 proposals).

**Next:** slice D (#16) engine wiring, control, restart reconcile, AT-01..12.

---

## M06 slice B — drawdown, protected policy, pre-trade checks, risk veto (2026-11-27, /tdd + /simplify + /code-review + /scrutinize + /security-review, branch `feat/14-m06-risk`)

**Goal:** the AI-independent decision on every trade proposal, and the account drawdown lock (PRD-06 #12, slice #14; V1.1 UST-03/06/08, CAT-X02-*).

**Shipped:** `backend/app/risk/{policy,drawdown,veto}.py`, `migrations/0003_risk.sql`. Policy values are PROPOSED and uncertified (`risk-0.1-uncertified`).

**Validation:** `uv run pytest` green; ruff + mypy clean. Mutation probes (Martingale behaviour, exits bypass, lot-below-min, lifetime approval, lock latch, cash flow) each turned the matching test red. Review fixes: exits never gated by Live revocation/mode; size-up check covers OPEN too and fails closed on unknown cycle P&L; read-modify-write of DD state in one transaction; malformed/mismatched spec and quote denied; future-dated quote is stale; partial close validated (incl. remainder); lifetime breach always reported; recovery approval audited; single lock flag; account identity (login, server); mode-restricted strategies in policy.

**Next:** slice C (#15) cycle book, target, trailing, close workflow.

---

## M06 slice A — broker port, fake broker, ownership, execution ledger (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize + /security-review, branch `feat/13-m06-broker-ledger`)

**Goal:** the single, durable path to the broker (PRD-06 #12, slice #13).

**Shipped:** `backend/app/execution/{broker,fake_broker,ownership,ledger,executor}.py`, `migrations/0002_execution.sql`, `RequestKind`.

**Validation:** `uv run pytest` → 138 passed; ruff + mypy clean. Mutation probes (unknown-outcome guard, ownership link check, netting ambiguity, quiet period, cancel ownership, broker-exception handling, duplicate send) each turned the matching test red.

**Review fixes:** netting open blocked when a non-bot position exists; reconcile never writes off an empty lookup before a quiet period (and never while disconnected); cancel requires a bot-placed order; broker exceptions record UNKNOWN; MT5-like commission and netting in the fake broker; deal-id cursor; VWAP fill price; ownership keyed by login/server/symbol.

**Not done:** the real MT5 adapter + MQL5 EA (#17, blocked: no MT5).

---

## M08 slice B — owner API, auth, control commands, live feed (2026-11-27, /tdd + /simplify + /code-review + /scrutinize + /security-review, branch `feat/9-m08-api`)

**Goal:** the authenticated local API and live feed the dashboard uses (PRD-08 #7, slice #9).

**Shipped:** `backend/app/api/{app,auth,__main__}.py`; journal read helpers (`recent`, `find`, `latest_per_cycle`, `latest_per_payload_field`, `payload_as`); `secrets.unredact`; shared `health.effective_health`; deps FastAPI, uvicorn, httpx (dev).

**Validation:** `uv run pytest` → 115 passed; ruff + mypy clean. Every private route is derived from the app's route table and checked for 401. Mutation probes: removing the router auth dependency, the throttle check, or the session-expiry comparison each turned the matching test red; removing both WS session checks made the WS test hang (caught, but as a hang rather than a clean failure).

**Not done:** the positions and reports read models (return `available: false` until M06/X02 exist); TLS/remote access (local only).

**Next:** M06 Risk & Execution.

---

## M08 slice A — storage, journal, config, lease, backup, health, logging (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, branch `feat/8-m08-storage`)

**Goal:** the durable local core every module shares (PRD-08 #7, slice #8).

**Shipped:** `migrations/0001_core.sql`; `backend/app/storage/{db,journal,flags,lease,backup,config_store,audit}.py`; `backend/app/core/{config,secrets,health,logging}.py`; `RiskProfile`, `Clock`, `utc_now_iso`.

**Validation:** `uv run pytest` → 76 passed; ruff + mypy clean. Five mutation probes (dedupe, offset monotonicity, lease exclusivity, restore reconcile flag, LIVE in owner config) each turned the matching test red. Review fixes: redaction of nested/list secrets, atomic reconcile marker on restore, migration splitting and concurrent-migrate race, transaction rollback/commit safety + savepoints, URI-safe backup paths.

**Next:** slice B (#9) API/auth/WS.

---

## X01 shared contracts + P1 Python foundation (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, branch `feat/5-x01-contracts`)

**Goal:** one typed vocabulary every module builds against (PRD-X01, issue #5).

**Shipped:** `pyproject.toml` + `uv.lock` (Python 3.13, pydantic, ruff, mypy strict, pytest); `backend/app/core/` — value types (Decimal, no float), enums, control/cycle state machines with the risk lock as a separate dimension, 18 event payloads + discriminated envelope, `Confidence`; `docs/contracts/*.schema.json` (generated); CI `lint`/`typecheck`/`test`; `verify` armed.

**Validation:** `uv run pytest` → 49 passed; ruff + mypy clean. Review findings fixed: entering RISK_LOCKED now sets the lock and RUNNING is refused while locked (was: lock lost → trading resumed without approval); bounded ALLOW; sized OPEN/ADD; candle look-ahead and range checks; schema_version pinned; mode required.

**Next:** PRD-08 system core.

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
