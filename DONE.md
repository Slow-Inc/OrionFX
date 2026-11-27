# DONE — Agent Session Log

> Newest entry on top. One dated `##` heading per shipped unit so an agent can jump to one.
> When this crosses ~a few hundred lines or a phase closes, move older entries to
> `DONE-archive-<period>.md` and leave a redirect line here.

---

## X01c slice A: an exit closes only the cycle it names (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/89-exit-cycle-guard`)

**Goal:** a late exit decided for an old cycle never closes the new cycle's positions, and risk can still always be reduced (PRD-X01c #88, slice #89).

**Shipped:**
- M06 refuses an exit (CLOSE/CLOSE_ALL) that doesn't name the open cycle (`CYCLE_MISMATCH`). The refusal is journaled under the cycle the exit named.
- An allowed exit closes only positions linked to the cycle it names.
- While no cycle is open (for example paused), an exit naming no cycle may close any Bot Position, including leftovers tagged with a closed cycle, so risk can always be reduced.
- Every `risk.decision` is filed under the cycle its intent named.
- Review fixes:
  - code-review: exits naming no cycle were blocked outright, and targets were not limited to the named cycle.
  - simplify: the test monkeypatching was replaced with a real cycle close, the filing rule no longer searches the reason list for a string, and a duplicate import was removed.

**Validation:**
- Backend verify green.
- Mutation probes: guard removed, None refused, and targets from any cycle are all caught.

---

## X01 follow-up C: runtime decision loop, plus the M06 entry guards it exposed (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/84-decision-loop`)

**Goal:** one tick runs M02 → evidence → M05 → journal → M06 → M07, never calls the broker, and decides nothing it cannot trust (PRD-X01b #81, slice #84; DEC-01, LRN-02, AT-14, AT-15).

**Shipped: `app.runtime.loop.DecisionLoop.tick()`**
- **Inputs:**
  - bars available at `now` for every configured timeframe; a missing one makes the snapshot MISSING;
  - price and spread from a fresh live quote (an injected provider), not the bar;
  - evidence from an injected provider.
- **M06 state, read from the journal:**
  - the open cycle, plus the last close, so the cooldown survives the next cycle opening;
  - the bot's positions from a fresh `positions.snapshot` that is newer than M06's last risk decision, in the open cycle, on one side;
  - intents M06 has not decided yet, in one bounded query (`Journal.unanswered`).
- **The decision:** `decide`, or `manage` when the bot holds a basket. The M07 experience is recorded *before* the intent is published, then due outcomes are labelled.
- **Fails closed:** any input not known gives no decision and health DEGRADED. A CLOSING cycle gets no decision. A fault gives DOWN with a 60 s backoff, and the fault report itself can't escape the tick.
- **Health:** journaled when it changes, plus a heartbeat every 60 s.

**M06 fixes found by scrutiny (pre-existing, made reachable by the loop):**
- Shadow no longer sends entries to the broker (`SHADOW_NO_ORDER`), per master plan "Shadow on live market (no broker order)".
- An entry that names no cycle or another cycle is refused (`CYCLE_MISMATCH`).
- A second OPEN is refused (`POSITION_OPEN`), and so is an ADD on the other side of the basket (`SIDE_MISMATCH`).
- M06 events are stamped with the engine clock, not the wall clock, so replay and cooldown are consistent.
- One `SNAPSHOT_STALE_AFTER` rule is now shared by the API and the loop.

**Validation:**
- Backend verify green.
- Mutation probes: all caught, after six tests were strengthened.

**Not done (ledger):**
- Running the loop in `serve` with real providers is blocked on #17, #41 and #48.
- Exit intents ignore `cycle_id`.
- Replay reads the latest state, not state as-of `now`.
- `Decision` doesn't carry direction and horizon.
- No `intent_id` index.

---

## X01 follow-up B: reasoning availability, judged point-in-time (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/83-reasoning-available-at`)

**Goal:** no look-ahead through LLM output (PRD-X01b #81, slice #83; AT-15).

**Shipped:**
- `LlmReasoning.available_at` is now required.
- One window rule, `require_window`, is shared by `NewsAssessment` (which had no check), `LlmReasoning` and `TechniqueObservation`.
- `reason()` and `assess_news()` stamp `available_at` from an injected clock *after* the answer returns. If the evidence expired during the call, they return DEGRADED instead of throwing.
- M05 fusion counts reasoning only while `live(available_at, valid_until, now)`.

**Validation:**
- `uv run pytest`: all pass.
- Mutation probes: all caught, including the window off-by-one and stamping before the call.

**Review fixes:**
- Stamping happens after the call, not before.
- An expired-during-call case gives a health result.
- One shared validator instead of three spellings.
- The fusion condition is a single line again.
- The contract test is in `test_catalog.py`, parametrized over both models.
- New boundary test: reasoning available exactly at `now` still counts.

**Note:** making `available_at` required doesn't break stored data, because `llm.reasoning` has never been persisted; nothing wires `reason()` into a running loop yet. The decision loop (#84) is the first writer.

---

## X01 follow-up A: bot positions read model (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/82-positions-read-model`)

**Goal:** the owner sees the bot's own positions, never a Manual Position, and never a stale or wrong list shown as fresh (PRD-X01b #81, slice #82; UI-02).

**Shipped:**
- Contracts `BotPosition` and `PositionsSnapshot`, with the event `positions.snapshot`. The master plan §8.2 row was added under X01 review, since §8 is a PROPOSED baseline.
- The engine publishes the snapshot every interval and also right after an execution. It never publishes from a disconnected terminal, and skips any snapshot with a position that can't be read cleanly.
- `OwnershipRegistry.bot_positions_with_cycle` checks ownership with one query, and still requires both the link and the magic number.
- `/api/positions` and `/api/account` share one helper that judges staleness at a single instant (`SNAPSHOT_STALE_AFTER`).
- The Trading view lists positions with their side, lot, open price, signed floating profit and freshness. Before the first report it says "not yet known"; it never says "none".

**Validation:**
- Backend verify green.
- Frontend tests (58) and build green.
- Mutation probes: all caught.

**Review fixes:**
- "Not yet known" is no longer shown as an empty list.
- A disconnected terminal publishes nothing.
- A snapshot is taken immediately after an execution.
- A malformed position can't crash the tick.
- The old table is hidden while a read error shows.
- Prices and lots are formatted as plain decimals.
- Ownership is one query; the dead `available` field is gone.

**Not done:** filtering snapshots by account/bot id is a pre-existing gap; it has a ledger row.

---

## M01 slice C: AI Insights, History and Settings (2026-11-27, /tdd + /simplify + /code-review + /scrutinize + /security-review, branch `feat/75-m01-insights`)

**Goal:** show what the AI did and why, report results honestly, and edit settings safely (PRD-01 #72, slice #75; UI-03/04/05, UST-09..11).

**Shipped:**
- `insights.tsx`:
  - The latest 30 decisions, with action, side, strategy, the EV estimate, reasons and evidence.
  - The latest 30 forecasts, with what the score means and whether it is Calibrated or Uncalibrated. A forecast is marked Expired when it is past its validity, or when the expiry can't be read.
- `history.tsx`: cycles with final realized net. Triggered and achieved are reported separately, and a closed cycle without a net shows "Not yet known".
- `settings.tsx`:
  - Saves with the version it read. A 409 offers a reload. A 422 shows errors on their fields and lists any that have no field on screen.
  - The secret is never shown or autofilled. The mask is echoed back unchanged, and the key can be removed.
  - The strategy list comes from the backend. Live is a disabled option with an explanation.
  - Save stays off until the saved version has been re-read.
- `ui.tsx`: shared `Money` and `ReadError`.
- Backend: `/api/settings` publishes `available_strategies`, and PATCH refuses unknown strategy ids with 422.

**Validation:**
- Backend verify is green.
- Frontend lint, typecheck, test (56) and build are green.
- Mutation probes: all caught.
- Security review: no findings.

**Review fixes:**
- No false conflict on a quick second save.
- Errors with no field on screen are listed.
- The API key field can't be autofilled with the dashboard password.
- A stored key can be removed.
- History no longer says "open" for a closed cycle.
- An unparseable expiry counts as expired.
- Lists say "latest 30" and use de-duplicated keys.
- The strategy list comes from the backend, which enforces it.
- Shared money and error components.
- A single outcome state in Settings.
- Shared test render helper.

---

## M01 slice B: Trading view (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/74-m01-trading`)

**Goal:** see and control the bot honestly (PRD-01 #72, slice #74; UI-02, UST-01). The Account DD is kept apart from the Cycle Net estimate, nothing stale looks live, nothing sent looks done, and there is no accidental Emergency Stop.

**Shipped:**
- Backend: `/api/status` gains `risk`, the latest Account DD reading with `observed_at`. The lock is reported once, at the top level.
- Frontend `trading.tsx`:
  - Account card and Account DD, each with freshness. An unknown DD shows as "Unknown", never 0.
  - Current cycle, with Cycle Net labelled an estimate and signed with a word.
  - Positions say "not available yet", with no stub fetch.
  - Controls are enabled only for a known state that allows them.
  - One `ConfirmButton` for every control. Emergency Stop sits in its own zone and needs STOP typed.
- `commands.ts`: `runCommand` stays PENDING until M06 answers. UNRESOLVED and REJECTED are shown, and a timeout reads STILL_PENDING. Polling is abortable and stops on sign-out.
- `snapshot.ts`: `useSnapshot`.
- `format.ts`: `formatPercent`.

**Validation:**
- Backend verify green.
- Frontend lint, typecheck, test (37) and build green.
- 16 mutation probes, all caught.

**Review fixes:**
- A second command can't be confirmed while one is pending.
- Start is not enabled while the state is unknown or wrong.
- The Account DD and the cycle show their age.
- Read errors show as errors.
- A malformed money value can't crash the view.
- Polling stops on unmount or sign-out.
- Emergency Stop's Cancel clears the typed word.
- One lock value in `/api/status`.
- "Approve Recovery" uses the glossary term.

---

## M01 slice A: frontend foundation (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/73-m01-foundation`)

**Goal:** a dashboard foundation that never pretends. Stale is never shown as Live, a failed sign-in is never shown as a lost session, and money never drifts (PRD-01 #72, slice #73; UI-01, UST-01).

**Shipped:** `frontend/`, a Bun + Vite + React 19 + TypeScript (strict) app with Biome and Vitest.
- `api.ts`: typed client with bearer auth. Errors have kinds (unauthorized / conflict / validation with fields / rate_limited / unavailable) and carry the server's own `detail`. `VITE_API_BASE` sets the API origin for builds.
- `live.ts`: `LiveFeed`.
  - Reports Live only after the hello is accepted.
  - Reconnects with backoff and resumes from the last seq.
  - 4401 and 4400 are final.
  - Malformed frames are ignored.
- `freshness.ts` (5 s skew tolerance), `format.ts` (integer cents, exponent notation, sign + word).
- `app.tsx`:
  - In-memory session; sign-in, sign-out and expiry are told apart.
  - Hash routes and a status bar that re-checks freshness every second.
  - Status is polled; refreshes are coalesced; the feed starts from `latest_seq`.
- DESIGN.md tokens drafted; CLAUDE.md frontend commands.

**Validation:**
- `bun run lint && bun run typecheck && bun run test && bun run build`: green, 19 tests.
- 16 mutation probes, all caught.

**Review fixes:**
- No full-journal replay on sign-in.
- Refresh storms are coalesced.
- An ageing status turns Stale on screen.
- A wrong password no longer shows "session ended".
- Live is not reported before the hello is accepted, and 4400 is not retried forever.
- "0E-8" money no longer crashes a view.
- Clock skew is tolerated.
- The API base is configurable.
- Sign-out is told apart from expiry.
- The 409 `detail` is kept for Start.

**Not done:**
- **Frontend CI jobs.** They need a `.github/workflows` and `.claude/t4.json` change, which are enforcement paths the owner reviews, so they go in a separate PR.
- Generating the TS types from `docs/contracts` schemas. The types are hand-mirrored for now; this is a follow-up.

---

## X02 slice C: evidence store, protected readiness gate, live qualifications (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/68-x02-readiness`)

**Goal:** Live approval rests on stored, protected evidence that nobody can type in or edit (PRD-X02 #65, slice #68; VAL-04/05/06, AT-16, AT-24).

**Shipped:**
- `migrations/0010_validation.sql`: runs, policies and qualifications, all append-only.
- `backend/app/validation/evidence.py`:
  - `ValidationRun`, a content-addressed `x02:run:` record.
  - `ReadinessPolicy`, with immutable versions. A certified policy needs criteria and regimes. `DEFAULT_POLICY` is uncertified and every threshold is TBD.
  - `EvidenceStore.evaluate`, which judges only against the current policy and reports every reason.
  - A qualification is issued only on READY, bound to the subjects it tested and to the policy version.
- `core.ids.content_hash`.
- M07 `ModelLifecycle(live_verifier=…)`. LIVE fails closed without the verifier, and the ref must belong to the model being promoted.

**Validation:**
- `uv run pytest`: all pass, including 11 readiness tests and 1 new lifecycle test.
- 10 mutation probes: 9 caught, 1 equivalent. A duplicated run merges into the same window cluster anyway, and is still flagged RUN_DUPLICATE.

**Review fixes:**
- Duplicated runs are refused.
- Coverage counts distinct evidence: windows are merged, gaps are not covered days, and overlapping runs count their trades once.
- The trade count is an explicit field.
- Evaluation always uses the current policy.
- `issue` is private.
- The qualification is bound to its subjects.
- Live fails closed without the verifier.
- A run window must end after it starts.
- An empty certified policy is refused.
- No duplicate audit row on re-evaluation.
- One prefix constant.
- Hash-based policy immutability.

**Skipped:** a shared "insert immutable version" helper across the registry, datasets and policies. The three have different conflict semantics and exceptions; noted as a candidate refactor.

---

## X02 slice B: point-in-time replay harness, cost model and AT-25 scenarios (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/67-x02-replay`)

**Goal:** replay history exactly as the bot would have seen it, through the real M02/M05 code, with every cost stated (PRD-X02 #65, slice #67; VAL-02, VAL-03, AT-15, AT-25).

**Shipped:**
- `backend/app/validation/replay.py`: `replay`, `CostModel` (explicit, non-negative costs), `SimTrade` (with `forced_exit`), `ReplayResult`, which includes the metrics, a no-trade baseline, per-step bar availability and the list of simplifications.
- `backend/app/validation/scenarios.py`: seeded AT-25 generators (strong trend, range, false breakout, reversal, news spike, wide spread).

**Behaviour:** with no calibrated forecast, every decision is WAIT and the result equals the no-trade baseline. That is intended.

**Validation:**
- `uv run pytest`: all pass, including 11 replay/scenario tests.
- 11 mutation probes, all caught.

**Review fixes:**
- Trades exit at their horizon bar. Before, they were held one bar too long because of the `available_at` offset.
- A position still open when the window ends is closed at the last bar, flagged, and counted.
- The last bar of the window is replayed.
- Lot and horizon come from the intent and from the proposal `decide` chose.
- Removed a fill clamp that never fired; non-negative costs give the same guarantee.
- History is loaded once per timeframe and sliced by `available_at` with bisect.
- The lookback is the slowest technique's own.
- Not simulating M05 `manage` while a position is held is now stated in the simplifications.

**Skipped:**
- Reusing `FakeBroker` accounting for replay fills. Replay settles per bar rather than per deal, and the rounding difference is at most a cent per trade. This is noted as a known divergence.
- Incremental technique computation for multi-year replays.

---

## X02 slice A: validation metrics and chronological walk-forward splits (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/66-x02-metrics`)

**Goal:** measure models and strategies honestly, with sample sizes, and split time without leakage (PRD-X02 #65, slice #66; VAL-02, VAL-04).

**Shipped:**
- `backend/app/validation/metrics.py`. Each metric returns its value and `n`, and an empty sample gives None. The metrics are:
  - direction accuracy, where a NEUTRAL outcome or a NEUTRAL (abstaining) prediction is skipped;
  - Brier;
  - reliability bins, single pass, where p = 1 lands in the last bin;
  - MAE/RMSE;
  - expectancy;
  - win rate, where break-even trades count as neither win nor loss;
  - profit factor, which is None without losses;
  - max drawdown, as an amount from 0, not Account DD %;
  - target hit rate.
- `backend/app/validation/splits.py`: `walk_forward` returns `WalkForward(folds, holdout)`. Folds roll by the test length, an embargo sits after each train window, and a second embargo sits before the sealed holdout.

**Validation:**
- `uv run pytest`: all pass, including 14 tests in `tests/unit/validation`.
- Mutation probes: all caught. One survivor came from a test message match that was too loose; the match is tightened.

**Review fixes:**
- Bins are computed as `i/bins`, so a certain forecast is not dropped at bins = 3, 6, 7, 9.
- Probabilities outside [0, 1] are refused, and so is `bins` < 1.
- An embargo now also separates the holdout.
- A reversed range is reported as such.
- `_rate` helper; the holdout is a named window.

**Skipped:**
- Returning the holdout as M07's `Split`: validation should not depend on learning. It uses its own `Window`.

---

## M07 slice C: dataset manifests, evidenced promotion, rollback and drift (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/61-m07-lifecycle`)

**Goal:** a controlled path from a trained model to the champion, without training anything and without anything promoting itself (PRD-07 #58, slice #61; LRN-04/05/06, AT-13, AT-16, AT-35).

**Shipped:**
- `migrations/0009_learning_lifecycle.sql`: datasets, candidates and promotions, all append-only.
- `backend/app/learning/lifecycle.py`:
  - `DatasetManifest`: splits must run train → validation → holdout in time order.
  - `DatasetStore`, which audits every save.
  - `ModelLifecycle` with:
    - `propose_candidate(ModelCard)`, which needs at least one dataset;
    - `promote`, owner only, which needs evidence and a policy, and a separate `x02:live-qualification:` ref for LIVE;
    - `rollback` over a champion stack, which refuses an expired approval;
    - `record_drift`.
  - `psi` and `drift_report`. A feature missing from either window counts as drift.
- `core.contracts`: `ELIGIBILITY_MODES` and `minimum_eligibility`, so there is one mode ladder.

**Validation:**
- `uv run pytest`: all pass, including 19 lifecycle tests.
- 21 mutation probes, all caught.

**Review fixes:**
- Owner check:
  - It is an allow-list (`owner`) instead of a deny-list.
  - It refuses m09, scheduler, owner-bot, "OWNER ".
- The LIVE gate uses a structured prefix ref, not a substring match.
- Split order is enforced.
- Candidates:
  - They are stored with their datasets.
  - A model that is registered but was never proposed cannot be promoted.
- Rollback:
  - A second rollback does not restore the version just rolled away from.
  - Rolling back to an expired approval is refused.
- An unsupported mode raises `LifecycleRefused`.
- PSI counts with bisect and Counter.
- `champion()` uses `LIMIT 1`.

---

## M07 slice B: point-in-time similarity retrieval, semantic memory, RAG context (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/60-m07-memory`)

**Goal:** recall similar past situations without look-ahead, and never present similarity as a win probability (PRD-07 #58, slice #60; LRN-01/02/03, AT-15).

**Shipped:**
- `backend/app/learning/memory.py`:
  - `similar`: RMS distance over the shared non-regime dimensions, in the same regime, needing at least 2 dimensions. It carries `SIMILARITY_SEMANTICS`.
  - `summarize`: kept apart by label kind, with moves counted relative to the side the decision took.
  - `rag_context`: a cited block that states each decision's side and whether the outcome went its way.
  - `with_side`.
- `ExperienceLog.known(as_of, regime)`: a SQL join, point-in-time, with UTC enforced.
- `migrations/0008_learning_one_label.sql`: at most one label per experience.
- Catalog: CAT-A04-03 is implemented, with no validated-edge claim. CAT-A04-04..07 are blocked, because they need training or simulation. CAT-A04-01 and A04-02 stay planned.

**Validation:**
- `uv run pytest`: all pass, including 14 memory tests.
- Mutation probes: all caught.

**Review fixes:**
- Regime flags no longer give free similarity.
- RMS distance, so matches with more shared features are not penalised.
- Context and summary judge outcomes against the side the decision took, not raw up/down.
- `k` < 1 is refused.
- A naive `as_of` is refused.
- One label per decision.
- An experience with no strategy is grouped under None, not '-'.
- The DTW catalog ref is corrected.
- The `candles` property added for tests is removed.
- A04-01 is not claimed, because its only evidence was vector similarity.

---

## M07 slice A: experience log and point-in-time outcome labels (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/59-m07-experience`)

**Goal:** turn every decision, WAIT included, into an experience with honest, point-in-time outcome labels (PRD-07 #58, slice #59; LRN-02/06, AT-15).

**Shipped:**
- `migrations/0007_learning.sql`:
  - `learning_experiences` and `learning_labels`, both append-only.
  - A trigger that freezes `final_net` once a cycle is CLOSED.
- `backend/app/learning/experience.py`:
  - `ExperienceLog` with `record`, `experiences`, `labels(as_of)` and `label_due(now)`.
  - `vector_of` and `regime_of`.
  - The `Experience` and `Label` contracts.
- `CandleStore.bar_at`.

**Behaviour:**
- REALIZED labels:
  - Only for an experience whose intent actually filled, meaning an OPEN execution with filled volume > 0.
  - Value: the cycle's final realized net, known at the cycle's close.
- MARKET_MOVE labels:
  - For everything else, including refused or unfilled ENTERs.
  - Value: the move from the bar closing at `as_of` to the bar closing exactly at the horizon end.
  - Known when that bar became available.
- A gap or missing data means no label.

**Validation:**
- `uv run pytest`: all pass, with 15 learning tests.
- 9 mutation probes: 6 caught, 3 equivalent (redundant horizon fast path, open cycles never have `closed_at`, a race prevented by `BEGIN IMMEDIATE`).

**Review fixes:**
- A refused or unfilled ENTER no longer gets a P&L label, because REALIZED now needs fill evidence.
- Labels use bars at exactly the horizon boundaries, never a stretched horizon over a gap.
- Labelling is bounded:
  - SQL filters on `horizon_end`.
  - Two `bar_at` lookups replace a full history load.
  - The scan runs inside the transaction, with ON CONFLICT DO NOTHING.
- `final_net` is frozen after close, so `available_at` is honest.
- `regime_of` reads the technique's own trend/range flags, adding NEUTRAL for ADX 20–25.
- Feature keys now come from `feature_key`, not M05 constants.
- `Label` is a contract with a stored body and a `labels(as_of)` reader.
- Candle refs come from `CandleStore.event_id`.
- The text says "final realized net", per the glossary.

**Skipped:** reading cycles through `CycleBook`. Its constructor needs an `AccountInfo` the read never uses; the raw read uses `CycleState.CLOSED`.

---

## M05 slice C — exits, bounded adds, recovery as opportunity, trace and replay (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/54-m05-exits`)

**Goal:** manage a cycle that holds the bot's basket, and make every decision reproducible from stored inputs (PRD-05 #51, slice #54; DEC-05/06/07, UST-12, AT-14, AT-41, AT-42).

**Shipped:**
- `backend/app/decision/fusion.py`: the shared core, covering gather, votes, EV, build/wait, events, `Costs`, `CycleView.pending` and `Evidence`.
- `manage.py`:
  - EXIT CLOSE_ALL when the evidence reverses.
  - ADD only as a positive-EV opportunity, at the initial lot and below the cap. A losing cycle gives RECOVERY_OPPORTUNITY, refused in Live. An unknown Cycle Net fails closed.
  - Otherwise WAIT HOLD.
- `trace.py`: a nested `DecisionTrace` with `capture` and `replay`.
- `decide.py` is now a thin policy on top of fusion.
- Tests:
  - `tests/support/{decision,engine}.py` hold the shared builders; tests no longer import each other.
  - The integration fixtures live in `tests/integration/conftest.py`.
  - New `tests/integration/test_decision_to_engine.py`.

**Validation:**
- `uv run pytest`: all pass.
- M05 → journal → M06 → fake broker works end to end: ENTER is allowed and opens a position, a republish is ignored, and CLOSE_ALL closes the basket.
- Mutation probes: 23 on manage/trace/pending, all caught.

**Review fixes:**
- Repeats across ticks are refused by pending intent action (OPEN/ADD/CLOSE_ALL), so floating Cycle Net or spread no longer slips a duplicate ADD/EXIT through. Cycle Net and lots are out of the decision id.
- Live forecast and news can call an exit on a stale snapshot; only the stale chart vote is dropped.
- The trace is nested. `max_positions` is required whenever there is a held basket, there is no `or 1` fallback, and a 0 cap is accepted. A test checks that every input field is captured.
- `CYCLE_PNL_UNKNOWN` is renamed `CYCLE_NET_UNKNOWN` to match the glossary.
- The docstring no longer claims that M06 refuses a same-lot Live recovery add.

**Not done:**
- Whether M06 should also refuse a same-lot Live recovery add is an owner decision. It is a ledger row.
- CAT-R02-* conditional recovery variants stay planned.

---

## M05 slice B — fusion, EV after costs, WAIT rules, decisions and intents (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/53-m05-decide`)

**Goal:** turn proposals and evidence into one explainable ENTER or WAIT, never trading on stale, conflicting or non-positive-EV evidence (PRD-05 #51, slice #53; DEC-01/02/04/05, AT-14/18/19/34).

**Shipped:** `backend/app/decision/decide.py` (`decide`, `to_events`, `Costs.from_spec`, `CycleView`, `Evidence`, `Decision`); `WaitReason` enum; `core.ids.stable_id` (canonical-JSON deterministic ids); `FeatureSnapshot.ref` (now shared by M03/M04/M05).

**Behaviour:** without an approved calibrated model, every decision is WAIT `EV_UNKNOWN`. That is by design: the system does not trade on an edge it cannot evidence.

**Validation:** `uv run pytest`: all pass, including 28 decide tests written before the code. 29 mutation probes, all caught.

**Review fixes:**
- Stale snapshot, expired proposal, non-OPEN or NEUTRAL proposal, and non-positive lot now WAIT.
- An LLM reasoning that restates the forecast or counted news, or that rests on stale or uncited news, gets no vote.
- News that netted to zero no longer suppresses a reasoning vote.
- p is used only when the forecast has OK data quality and the trade's horizon. Unusable forecasts neither vote nor are cited.
- EV is computed only after every other gate passes.
- Lot, spread and costs are now part of the decision id.
- Events take mode and cycle from the decision, so WAIT events keep their cycle.
- The two CONFLICT cases now have separate reason codes.

**Skipped:**
- A shared `CENT` constant; money rounding in core is left for a later slice.
- A shared decision test fixture module.
- `LlmReasoning` has no `available_at`, so it has no look-ahead guard. This is a contract gap, recorded here.

---

## M05 slice A — strategy port, registry gate, MVP rule strategies (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/52-m05-strategies`)

**Goal:** only owner-enabled, registry-approved strategies propose, as documented deterministic rules (PRD-05 #51, slice #52; DEC-03, UST-01/06/08, AT-28, AT-32).

**Shipped:** `backend/app/decision/{context,strategies,gate}.py`; `feature_key` + single `_key` format in M02 features; `EligibilityRecord.approves(mode, now)` (now shared by gate and M04 engine); `ArtifactRegistry.eligibility_at` (point-in-time); `StrategyProposal.decision_id` optional until adopted. Catalog CAT-S01-01..06 → tested (rule logic on synthetic snapshots, thresholds PROPOSED, no performance claim); CAT-S01-07 → blocked (needs trained model).

**Validation:** `uv run pytest` → all pass. 18 mutation probes; 17 caught, 1 equivalent (`user_enabled` term in `approves`: the validator already forces disabled records to approve no modes).

**Review fixes:** hybrid votes only with members the gate allowed (was a consent bypass); gate refuses an approval whose hash differs from the running code, and each strategy's hash covers only its own rule + thresholds (newline-normalised); eligibility judged point-in-time at `ctx.now`; range no longer buys a breakdown or sells a breakout; breakout needs only the level being tested; pullback DOWN tested; per-strategy gate test.

**Skipped:** dropping the config `strategies_enabled` toggle in favour of only the record's `user_enabled` (both consents are required deliberately; documented in the gate).

---

## M04 slice B — adaptive ensemble (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/47-m04-ensemble`)

**Goal:** combine forecasts by evidence without double counting (PRD-04 #45, slice #47; PRE-03/04, AT-17).

**Shipped:** `backend/app/prediction/ensemble.py` (`ensemble`, `EnsembleResult`, `WiringError`); `ModelCard.skill`, shared `unique_leader`. Catalog CAT-A01-03 → implemented (synthetic members only).

**Validation:** `uv run pytest` → all pass (13 ensemble tests, written first). 15 mutation probes caught.

**Review fixes:** mismatched prediction/card pairs refused; models on the same inputs are one vote (max weight, not sum); latest repeat wins regardless of order; horizon is a parameter (other horizons skipped, not fatal); semantics state the horizon; metric names owned by the card.

**Skipped:** a shared `conftest.py` card factory (two small fixtures differ on purpose).

---

## M04 slice A — predictor port, model cards, approval-gated loading, baseline (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/46-m04-predictor`)

**Goal:** inference that runs only reviewed models on usable inputs and never mislabels a score (PRD-04 #45, slice #46; PRE-02/04, AT-16, AT-17). No training.

**Shipped:** `backend/app/prediction/{card,baseline,engine}.py` (`ModelCard`, `Predictor`, `BASELINE`, `load_model` via M08 `ArtifactLoader`, `load_baseline`, `LoadedPredictor`, `predict`, `Refused`); M02 `snapshot_problem`/`USABLE_QUALITY` (used by M03 and M04). Catalog: CAT-A01-01/02/04/05 blocked (no trained model).

**Validation:** `uv run pytest` → all pass (25 prediction tests, written first). 13 mutation probes caught.

**Review fixes:** model bytes verified by `ArtifactLoader` (the card's own hash is no longer trusted; the verified hash is stamped from the registry); only a `LoadedPredictor` can predict; score = the model's score for the predicted direction, ties refused; news-enhanced refused until news features exist, news passed to a chart-only model refused; NaN/float/exception refused; horizon from `snapshot.as_of`; shared snapshot rule.

**Not done:** trained XGBoost/LightGBM artifacts and adapters (#48). Ensemble is slice B (#47).

---

## M03 slice C — LLM reasoning stage and shared bounded LLM call (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/40-m03-reasoning`)

**Goal:** an evidence-linked `llm.reasoning` combining news, forecasts and the chart summary, with the same safe failure behaviour as assessment (PRD-03 #37, slice #40; LLM-04).

**Shipped:** `backend/app/news/reasoning.py` (`reason`, `ReasoningResult`); `llm_call.py` (`ask`, `health`, `load_json`, `COMPONENT`, `NO_PROVIDER`) shared by assessment and reasoning; `core.time.live`; M02 `SUMMARY_KEYS`; `LlmReasoning.feature_snapshot_ref`.

**Validation:** `uv run pytest` → 470 passed, 1 skipped (written first). 15 mutation probes caught.

**Review fixes:** no look-ahead on the snapshot (`available_at`); schema-checked snapshot; stale-data forecasts excluded; chart summary from M02's published keys (single-candle direction excluded); duplicate citations collapsed; snapshot used is recorded; invented ids reported as a count only; any adapter exception degrades (type name only).

---

## M03 slice B — provider port, untrusted-content policy, news assessment, degraded mode (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/39-m03-assessment`)

**Goal:** turn news into an evidence-linked `news.assessment` without giving the model any power, and wait safely when it fails (PRD-03 #37, slice #39; LLM-01/03/04/05).

**Shipped:** `backend/app/news/provider.py` (`Provider` port with no tools, `FakeProvider`, `ProviderError`/`ProviderTimeout`); `injection.py` (`build_prompt` with untrusted blocks, `injection_flags`); `assess.py` (`assess_news`, `ANSWER_FORMAT`, `AssessmentResult`); safe `NewsItem.item_id` charset.

**Validation:** `uv run pytest` → all pass (64 news tests, written first). 21 mutation probes caught (survivors fixed with per-pattern and unknown-stance tests).

**Review fixes:** item ids cannot carry prompt text; narrow patterns (ordinary financial wording not flagged); any delimiter spelling flagged; no look-ahead (`fetched_at <= now`); nested JSON degrades instead of crashing; provider error text never reaches health; all-injected batch is DEGRADED; fenced JSON accepted; field named in invalid-answer detail; conflict judged on cited items; answer format and validator in one place.

**Not done:** real LLM provider adapters (#41, no API key). CAT-A02-01/02 stay blocked.

---

## M03 slice A — news sources, provenance, cache, dedup, schedule (2026-11-27, /to-prd + /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/38-m03-news-sources`)

**Goal:** honest news input: provenance on every item, repeats never counted as independent evidence, fetching on a calendar/event schedule (PRD-03 #37, slice #38; LLM-02/03/05).

**Shipped:** `backend/app/news/{sources,cache,schedule}.py` (`NewsItem`, `NewsSource` port, `FixtureSource`, `NoSource`, `NewsCache`, `fingerprint`, `normalize_url`, `CalendarEvent`, `FetchPolicy`, `should_fetch`); `EventImpact` enum.

**Validation:** `uv run pytest` → all pass (23 news tests, written first). 20 mutation probes caught (one survivor fixed with a URL-variant test).

**Review fixes:** cursor on `fetched_at` (late-delivered stories not lost); cache keyed by (source, item_id); repeats extend validity; expired stories pruned (a repeat after expiry is live again); bare headlines dedup by URL only; normalized URLs; publisher clock skew tolerated; naive times refused; a clock that moved back fetches at once; `sources` always starts with the item's own source.

**Not done:** real news/calendar adapters (#41, no licensed source).

---

## M02 slice D — point-in-time candle store and replay (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/32-m02-store-replay`)

**Goal:** "what did we know at T" for backtests, and replays that reproduce live features (PRD-02 #28, slice #32; DAT-04, AT-15).

**Shipped:** `migrations/0006_candles.sql` (per dataset version; UPDATE/DELETE/REPLACE blocked); `backend/app/market_data/store.py` (`CandleStore.save/bars(as_of=)`, `event_id`); `backend/app/market_data/service.py` (`ingest`: one transaction per tick, deterministic `candle.closed` id and `occurred_at`); `make_event(event_id=, occurred_at=)`; `ClosedBar.partial`.

**Validation:** `uv run pytest` → all pass (AT-15: two replays and a live engine give identical snapshots; identical events across replays). Tests written first. 16 mutation probes caught (survivors on the first pass fixed with a crash-repair test and an H1-conflict atomicity test).

**Review fixes:** a bar the engine started mid-interval is partial and never stored (resume cannot freeze a truncated bar); a conflict rolls back the whole tick; `as_of` normalized to UTC, naive refused, `limit=0` means none; as-of query served by the index; INSERT … ON CONFLICT path.

**Config (developer request):** `.claude/t4.json` `autoMerge: true` so AFK merges do not stop to confirm (verify + green CI still enforced). Ledger row added to turn it back off when the AFK batch ends.

---

## M02 slice C — price-action techniques CAT-T03 + feature snapshot (2026-11-27, /tdd + /simplify + /code-review + /scrutinize, security-review n-a, branch `feat/31-m02-price-action`)

**Goal:** non-repainting price action and one versioned, point-in-time feature snapshot for M04/M05 (PRD-02 #28, slice #31; DAT-02, DAT-04, UST-03).

**Shipped:** `backend/app/market_data/techniques/price_action.py` (`make_price_action`: bar shape, patterns, S/R, structure, swings sharing one window, regime, per-timeframe direction); `backend/app/market_data/features.py` (`build_feature_snapshot`, schema `m02.features.v1`, `mtf.aligned`); `Technique.outputs`, `adx_lookback`, `OptValues`. Catalog CAT-T03-01..07 → tested (replay data only).

**Validation:** `uv run pytest` → 380 passed, 1 skipped. Tests written first (red on import). 29 mutation probes caught (3 survivors on the first pass fixed with new tests).

**Review fixes:** fixed key set in every data state; "no level found" no longer degrades the snapshot; CAT-T03-07 runs through `observe` (guards + lineage) and `aligned` is None when a direction is unknown; parameters in technique ids; one swing window for S/R/structure/swings; bars checked against their timeframe key; a requested timeframe with no bar is MISSING; tests for every output (bar shape, regime range/volatile, all patterns).

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
