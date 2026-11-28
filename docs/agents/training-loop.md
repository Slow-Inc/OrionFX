# M04 training loop: how it works

Status: **LIVING** (2026-11-28). This is the operating manual for the autonomous training loop in PRD #120, under ADR 0001 and the research grant in `CLAUDE.md` → "Training-loop research autonomy". Each part is marked **[built]** (merged, with tests), **[planned #n]** (an open issue) or **[decision]** (designed here, not yet built). Update the marks when a slice merges. Never mark something built without the PR that built it.

## 1. The loop at a glance

```
 ┌───────────── data ─────────────┐   ┌──────────── research cycle (development data) ───────────┐
 │ historical M1 import [#128]     │   │ hypothesis → config → train per fold → evaluate per fold │
 │ live bars (CandleEngine) [built]│──►│   ↑                                         │            │
 │ MarketRecorder → M07 dataset    │   │   └──── findings (docs/research) ◄──────────┘            │
 │   snapshots/outcomes [built]    │   └──────────────────────────┬───────────────────────────────┘
 └─────────────────────────────────┘                              │ budget spent or a config passes
                                                                  ▼
 ┌──────────── claim (pre-registered protocol version) ───────────────────────────────────────────┐
 │ freeze protocol → power check → system backtest → open holdout ONCE → readiness (per subject)   │
 └──────────────────────────────────────────────┬────────────────────────────────────────────────────┘
                                                │ all tiers pass
                                                ▼
 ┌──────────── deployment (ADR 0001) ───────────────────────────────────────────────────────────────┐
 │ candidate → shadow predictions beside champion → auto-promote to Shadow/Demo → forward monitor  │
 │   ↳ degradation or drift → auto-rollback, and a new protocol version on forward data only       │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Data layer

| Step | Mechanism | Status |
|---|---|---|
| Live bars | `CandleEngine` builds M5/M15/H1 from bid ticks and flags real gaps. With a `SessionSchedule`, scheduled breaks and weekends are not gaps. | [built] #29, #124 |
| Session schedule | `SessionSchedule` uses the New York zone and is DST-aware. `XAUUSD_NY_1700` is [PROPOSED]. `audit(schedule, bars)` checks it against the data. Each data source has its own schedule. | [built] #124. Per-source wiring: [planned #128] |
| Historical bars | Import M1 bars (Dukascopy first) and resample to M5/M15/H1, with spread and a **data-source** tag separate from LIVE/BACKFILL. Validation matches live bars, and chunks are resumable. | [planned #128] |
| Features | `build_feature_snapshot`, schema `m02.features.v2`, point-in-time, session-aware. | [built] #31, #124 |
| Per-bar dataset | `MarketRecorder` records one snapshot per closed bar (LIVE or BACKFILL) into `learning_snapshots`, in one transaction per pass. | [built] #108 |
| Outcomes | `BarLog.label_due` measures the move on the finest timeframe with bars closing at both ends. A row that cannot be labelled is marked once, point-in-time (`learning_unlabellable`). | [built] #108, #125 |
| Recorded predictions | Candidate and champion predictions per bar go to `learning_predictions`. They are learning data only and never trade. | [built] #109 |
| Export | `export_dataset` writes sorted, deterministic JSONL plus a hashed manifest. News is joined at `as_of` and leaking rows are purged. | [built] #110. Splitter folds, embargo and `available_at <= split.end`: [planned #128] |

**Rules for the data layer:**
- Data is point-in-time everywhere: a row uses only what was available at its `as_of`.
- Raw vendor data is never committed.
- News features are only `news.assessment` rows recorded live. The LLM never re-interprets past news, because of look-ahead through its training cutoff.

## 3. Labels and features for training

| Item | Definition | Status |
|---|---|---|
| Triple-barrier label | `app/learning/labels.py`, `m07.labels.tb.v2`. One label per bar, the same for both sides. Barriers at the entry bid close ±d on the bid path, d = `TARGET_ATR`·ATR_M15 (the recorded snapshot's ATR at `as_of`). UP: a bar's high reaches the upper barrier first; DOWN: a bar's low reaches the lower one first; FLAT: neither within N expected slots of the session schedule, N = the forecast horizon's span (a scheduled closure is not a gap). No spread, slippage or commission inside the label, so it depends on bid prices only and a source without spreads labels as live bid candles do. A bar that opens beyond a barrier (a gap) crossed it first and is labelled by that barrier. Any other bar touching both barriers is AMBIGUOUS: no class label, left out of the export. The export manifest's `label_stats` records `dual_touch_count`, `dual_touch_rate` and `flat_share` per era (each split, and "all"). Read on the finest stored timeframe that measures it (M5 first) so dual touches stay rare. A missing bar before it resolves gives no label. `K` = `TARGET_ATR` is [PROPOSED]; the constants EV shares live in `app/core/bet.py`. tb.v1 (side-dependent, spread inside) was replaced before anything trained on it. | [done #129] |
| Stationary transforms | `app/prediction/transforms.py`, `m04.transforms.v1`. One `transform` (via `transform_from_store`) called by the dataset export and by the recorder for a model whose card reads that schema. Per timeframe: `(close−sma)/atr`, Bollinger position, `macd/atr`, MACD histogram/atr, `atr/close`, the log return, `spread/atr`, RSI/ADX/±DI scaled to 0..1; `sin`/`cos` of the UTC hour. Session and liquidity features from the gold bars, point in time and session-aware: distance to the previous session's high and low in ATR_M15, the previous session's London/NY volatility ratio (local 08:00–17:00 each), spread over its rolling median. `tick_volume` is excluded. Invariant to a rescaled price; all but `atr/close` and the log return also to a shifted one. | [done #129] |
| Offline/online parity | The recorder and the export share `transform_from_store` and must use the same `SessionSchedule`: `export_dataset` and `prediction_labels` take `session` with no default, and the manifest records it as `session:<schedule_id or 24x7>` (XAUUSD: `XAUUSD_NY_1700`). Each exported barrier label carries `ends_at`; a walk-forward fold purges on it, not on `as_of + horizon`. `prediction_labels` scores recorded predictions on the label version the export carries (`barrier_labels`). The raw `move` stays as raw data. | [done #129] |

**Certified by agent under owner delegation, 2026-11-28 (#129).**
- **Dual touches stay excluded.** AMBIGUOUS rows are left out. Labels resolve on the finest stored timeframe (`FINEST_FIRST`, M5 first), so dual touches should be rare, and the manifest measures the rate.
- **Pre-declared switch.** If the measured `dual_touch_rate` exceeds 1% in any era, the next label version adds AMBIGUOUS as a fourth class. That class is priced worst case for both sides: `EV = size·(d·(p_s − p_o − p_amb) − fills) − commission`. The switch is declared now and not built in tb.v2.
- **FLAT drift = 0 is a certified assumption.** EV prices a FLAT at zero drift, and a FLAT still pays the fills. The label keeps the real drift as data. Estimating E[move | FLAT] is backlog item 9.

**EV prices the label, and both are proxies (#129).** M05's EV for side s uses the forecast's calibrated class probabilities of `LABEL_VERSION` (p_s = up for a long, down for a short): `EV = size·(d·(p_s − p_opposite) − (spread + 2·slippage)) − commission`, d = `TARGET_ATR`·ATR_M15, the spread the decision-time one (the stressed cost in backtests). That is `size·d·(p_s − p_opposite − cost_over_atr)`: every cost once, on every outcome, a FLAT's drift taken as 0. One triple prices both sides. EV at each certain outcome equals what that outcome pays the side after the same costs (`tests/unit/decision/test_ev_label_alignment.py`). `cost_over_atr` is the per-era break-even and `STRESSED_COST` the pre-declared stressed cost. **The system has no per-position SL/TP (RSK-05):** real exits are the Emergency SL, the USD cycle Profit Target and basket trailing. The ±1·ATR label and EV are a proxy for that trade; only the system backtest (#131), which replays the real exits, can judge whether a model is usable.

## 4. Training

| Item | Mechanism | Status |
|---|---|---|
| Trainer | LightGBM per fold (XGBoost as an alternative) with fixed seeds. It is an offline module; the runtime predictor stays Decimal at its boundary. | [planned #130] |
| Calibration | Isotonic or Platt, on a slice separated in time from the fold's training data. | [planned #130] |
| Metric baseline | Multiclass Brier against class priors from **that training fold only**. | [planned #130] |
| Artifact | Native model file (never pickle). The manifest records the commit SHA, dataset version, label and transform versions, seeds and library versions. | [planned #130] |
| Card | `ModelCard` gains a training cutoff, dataset ref, label version and transform version. `predict` refuses any snapshot with `as_of <= cutoff`, so a model is never scored in-sample. | [planned #130] |
| Trusted adapter | A reviewed module in an allowlisted package loads the weights only after checking them against the sha256 pinned in the registered card. One adapter serves many versions. | **[decision]** in the tracer PR #127: decide whether `build` receives the card and the verified weight bytes. Generating a module per version would break "allowlisted packages hold reviewed code only". |
| Evaluation load | `load_model` is point-in-time since #126, so a model registered today is refused for every historical `now`. An explicit **evaluation load** is hash-verified, needs no eligibility, and is marked so that no live path accepts it. | **[decision]** #127 |
| Where it runs | Colab CPU high-mem. Probed at 8 vCPU, 50 GB RAM, 226 GB disk and 0.26 CU/h. The code arrives as a `git archive` of a pinned commit, the VM holds no secrets, and only the artifact and manifest come back. Locally they are registered only after a hash check. Every VM is stopped after use. | [planned #134] |

## 5. Evaluation tiers (always reported separately)

1. **Model metrics per fold:**
   - multiclass Brier skill;
   - ECE;
   - direction accuracy with a block-bootstrap 95% CI over trading days;
   - precision at high confidence, with coverage;
   - per-regime buckets, each with `n`; thin buckets get no verdict. [planned #130/#132]
2. **System backtest.** `DecisionLoop`, M06 and `FakeBroker` run over stored bars on a fresh DB per run. The clock follows bar availability. The model's forecast reaches M05 through a **model-backed evidence provider**; the snapshot is passed in, not rebuilt (**[decision]** #127). Reported:
   - expectancy after costs, PF, max DD and trade count;
   - open positions at fold end and refused trades, reported separately.

   Two costs are measured, not assumed: M06's deal scan and the 30 s staleness rule after a DENY (#121). [planned #131]
3. **Shadow/Demo on the real market.** These are the same metrics on forward data. The only renewable evidence. [blocked #17]

The synthetic scenarios (`validation/scenarios.py`) are behaviour tests. They never count as edge.

## 6. The claim protocol (anti-overfitting)

A **protocol version** is a hashed record saved before the first run of a claim [planned #132]. It holds:
- the targets [PROPOSED; certified by the owner];
- the configuration space and the iteration budget (20);
- the regime rule;
- the fold plan;
- the debug window;
- the **unread slice**;
- the holdout window.

Rules, adopted from mip-research (PRD #120):
1. **A window that has been read is development data.** Walk-forward scores used to choose a config are development scores. The holdout opens once, by a recorded step, and is development data afterwards.
2. **Count independent units.** CIs use block bootstrap over trading days, and every count states how many days and episodes stand behind it.
3. **Check power before spending a window.** Do not open the holdout, or start Shadow/Demo, if the pass rule cannot pass at that size.
4. **Correct for selection.** Compare against a baseline rerun on the same folds and seeds, and report best-of-N together with N.
5. **Compare in pairs against a noise floor.** An A/A run measures seed variance; a difference below it is "unconfirmed".
6. **Recompute independently.** A second implementation recomputes the metrics from the logs, and a train/serve skew check compares feature vectors.
7. **Retention.** A new version must not do worse than the previous one on a frozen benchmark period.
8. **Keep errata.** Evidence can be marked withdrawn, corrected, unconfirmed or superseded.
9. **Say who verified.** Every report states what was verified and by whom.
10. **Stop rule.** If the budget runs out before the targets are met, stop and report. Never lower the targets.

**Readiness** is judged per subject: `EvidenceStore` qualifies only subjects common to every run [built #126]. `DEFAULT_POLICY` is uncertified, so the verdict is NOT_READY until the owner certifies thresholds.

## 7. Deployment and the closed loop (ADR 0001)

| Step | Mechanism | Status |
|---|---|---|
| Candidate | A model that passes every tier under a certified policy is proposed through `ModelLifecycle.propose_candidate`. | [built] #61 (lifecycle); wiring: [planned #133] |
| Shadow scoring | The candidate's recorded predictions run beside the champion's on live bars. A champion-vs-candidate report compares them on forward data. | [planned #133] |
| Promotion | A **policy-bound system actor** promotes to Shadow/Demo only under a certified policy version with the full evidence set. LIVE stays owner-only. | [planned #133] |
| Rollback | Automatic when forward metrics breach the policy floors: back to the previous champion, or to no model (M05 waits). It never rolls forward to an unqualified model. | [planned #133] |
| Drift | `psi` per feature above RETRAIN_PSI [PROPOSED] recommends a retrain. That opens a **new protocol version** evaluated on forward data only. | psi [built] #61; trigger [planned #133] |
| Independent safety | M06 checks registry eligibility itself, so whoever promoted a model or strategy, a missing, expired or revoked approval is refused. | [planned #121] (BLK-04) |

## 8. Research cycle (what the agent does on its own)

1. **Pick a question from the data.** For example: which features drift, which regime buckets are weak, whether a label horizon is wrong, or whether calibration is off.
2. **Write a short hypothesis** in `docs/research/` before running anything.
3. **Explore on development data only**, inside the declared budget.
4. **If it helps, ship it as a normal unit of work:** issue → TDD → T4 gates → PR. Generated code is untrusted until reviewed. It runs with no credentials.
5. **Turn a result into a claim** only through a new protocol version. The owner certifies any change to targets or thresholds; the agent may propose them, with evidence.
6. **Record the outcome either way.** Failures are evidence too, and wrong numbers go to errata.

## 8a. Research backlog (ordered; each item is an arm, never a shortcut)

Source: `docs/research/2026-11-28-m04-quant-upgrade-proposals.md`.

**Governing rule.** Every item below is an **arm** ablated against the V1 baseline on the same folds and seeds, under one pre-registered protocol version.
- **Shared budget.** All arms share the protocol's single iteration budget. Horizon, time decay, feature set and label type each count as configs.
- **One winner.** Only one winner per protocol version reaches the single-use holdout.
- **Baseline first.** Nothing here runs before the baseline exists (#127 → #130 → #132).

| Pri | Item | Where | Status |
|---|---|---|---|
| 0 | **EV and the label price the same event.** EV assumes a symmetric ±`TARGET_ATR`·ATR_M15 bet (`fusion.expected_value`). Triple-barrier barriers, the treatment of FLAT, the horizon and any meta-label P(win) must match it, or EV must change with them. | #129 | [done: tb.v2, literal EV, policy-0.5] |
| 1 | **Session and liquidity features** from the gold bars: distance to the previous session's high and low, the London/NY volatility ratio, spread anomalies. | #129 | [done] |
| 2 | **Uniqueness weights and time decay.** Pre-register whether claim metrics are weighted. Time decay is a hyperparameter inside the budget. | #130 | [planned] |
| 3 | **Horizon M15–H1 as configs.** Watch label overlap. | #130/#132 | [planned] |
| 4 | **Volatility-model WAIT gate.** Block-only, following the `NEWS_VETO` pattern. New evidence contract. Baseline is persistence plus the M02 regime rule. | #139 | [planned] |
| 5 | **Meta-labeling arm.** Predictor-port contract change. Needs an evaluation path for strategy eligibility over history (the same seam as #127). The dataset pins the strategy rule hash. | #140 | [planned] |
| 6 | **Cross-market data-only symbols** (XAGUSD and a USD proxy). Schema bump, never tradable, one session schedule per symbol. Admitted only with a live source. Measure the skew between the training feed and the serving feed. | #141 | [planned] |
| 7 | Yields, VIX and the pre-event countdown. Need point-in-time sources. | after #41 / OD-14 | [blocked] |
| 8 | Chart+News fusion and regime-specific ensembles, from Shadow data. | after #17, #41 | [blocked] |
| 9 | **Estimate E[move \| FLAT].** EV prices a FLAT at zero drift (certified assumption, #129). Measure the FLAT drift per era from the exported `barrier_labels` and, if it is material, propose a label/EV version that prices it. | after #130 | [planned] |
| — | **Model input to M06 trailing, TP or SL.** An owner decision, never a backlog item ("risk is independent of AI"). | owner | [not planned] |
| — | Deep learning or transformers on raw OHLCV. Deferred. | — | [not planned] |

## 9. Who decides what

| Decision | Owner | Agent |
|---|---|---|
| Targets, readiness thresholds, holdout window, policy certification | ✅ | proposes |
| Features, labels, model family, hyperparameters, pipeline code | reviews by PR | ✅ within the protocol |
| Promotion and rollback in Shadow/Demo | certifies the policy once | ✅ automatic (ADR 0001) |
| Real-money Live | ✅ only | never |
| External resources: MT5, Demo account, news licence | ✅ | — |

## 10. Open design decisions (resolve in #127)

- **Evaluation load path** for historical scoring: no eligibility needed, and never accepted by a live path.
- **Adapter and weights:** how `build` gets the card and the verified weight bytes, with one reviewed adapter for many versions.
- **Evidence provider input:** it receives the snapshot instead of rebuilding it.
- **Tracer fixture:** a daily break, a weekend, M5 recording and M15/H1 horizons. It asserts usable features, label coverage and at least one ENTER in the test fold. A negative control with `session=None` must fail. Readiness must be NOT_READY under the default policy.
