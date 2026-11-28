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
| Triple-barrier label | `app/learning/labels.py`, `m07.labels.tb.v2`. One label per bar, the same for both sides. Barriers at the entry bid close ±d on the bid path, d = `TARGET_ATR`·ATR_M15 (the recorded snapshot's ATR at `as_of`). UP: a bar's high reaches the upper barrier first; DOWN: a bar's low reaches the lower one first; FLAT: neither within N expected slots of the session schedule, N = the forecast horizon's span (a scheduled closure is not a gap). No spread, slippage or commission inside the label, so it depends on bid prices only and a source without spreads labels as live bid candles do. A bar touching both barriers is AMBIGUOUS: no class label, left out of the export, its rate reported by `LabelStats` (with the FLAT share) per era. Read on the finest stored timeframe that measures it (M5 first) so dual touches stay rare. A missing bar before it resolves gives no label. `K` = `TARGET_ATR` is [PROPOSED]; the constants EV shares live in `app/core/bet.py`. tb.v1 (side-dependent, spread inside) was replaced before anything trained on it. | [done #129] |
| Stationary transforms | `app/prediction/transforms.py`, `m04.transforms.v1`. One `transform` (via `transform_from_store`) called by the dataset export and by the recorder for a model whose card reads that schema. Per timeframe: `(close−sma)/atr`, Bollinger position, `macd/atr`, MACD histogram/atr, `atr/close`, the log return, `spread/atr`, RSI/ADX/±DI scaled to 0..1; `sin`/`cos` of the UTC hour. Session and liquidity features from the gold bars, point in time and session-aware: distance to the previous session's high and low in ATR_M15, the previous session's London/NY volatility ratio (local 08:00–17:00 each), spread over its rolling median. `tick_volume` is excluded. Invariant to a rescaled price; all but `atr/close` and the log return also to a shifted one. | [done #129] |
| Offline/online parity | The recorder and the export share `transform_from_store`; `prediction_labels` scores recorded predictions on the label version the export carries (`barrier_labels`). The raw `move` stays as raw data. | [done #129] |

**EV prices the label, and both are proxies (#129).** M05's EV for side s uses the forecast's calibrated class probabilities of `LABEL_VERSION` (p_s = up for a long, down for a short): `EV = size·(d·(p_s − p_opposite) − (spread + 2·slippage)) − commission`, d = `TARGET_ATR`·ATR_M15, the spread the decision-time one (the stressed cost in backtests). That is `size·d·(p_s − p_opposite − cost_over_atr)`: every cost once, on every outcome, a FLAT's drift taken as 0. One triple prices both sides. EV at each certain outcome equals what that outcome pays the side after the same costs (`tests/unit/decision/test_ev_label_alignment.py`). `cost_over_atr` is the per-era break-even and `STRESSED_COST` the pre-declared stressed cost. **The system has no per-position SL/TP (RSK-05):** real exits are the Emergency SL, the USD cycle Profit Target and basket trailing. The ±1·ATR label and EV are a proxy for that trade; only the system backtest (#131), which replays the real exits, can judge whether a model is usable.

## 4. Training

| Item | Mechanism | Status |
|---|---|---|
| Trainer | `app/learning/trainer.py` `train(rows, LightGBMConfig, dataset_ref=, code_commit=)` on an `export_dataset(transforms=True)` file. LightGBM `multiclass` (softmax over UP/DOWN/FLAT), shallow and regularised; one booster per seed (default 5), averaged; rows weighted by label **uniqueness** (mean 1/concurrency over `as_of`→label `available_at`); no class rebalancing. Early stopping on the last 15% of the train split, purged of labels still open when it starts; the early-stopped booster is kept. `LightGBMConfig` is a frozen typed object; its canonical-JSON sha256 goes on the card. Leak checks before fitting: features must be M04 transform keys, and no single feature may predict the label almost perfectly (`LeakSuspected`, the future-feature canary). Offline, on floats; the runtime adapter stays Decimal at its boundary. XGBoost is not built. | [built #130] (branch `feat/130-lightgbm-trainer`) |
| Calibration | One temperature on the `validation` split (purged by the export, immediately before the test window), fitted by weighted log loss through the adapter's own `calibrate`: softmax(log(mean of the seeds' probabilities) / T). Isotonic is **not built**: it needs n_eff ≥ ~10k on the calibration slice. | [built #130]; isotonic [not built] |
| Metric baseline | `app/learning/evaluation.py`: multiclass Brier and Brier skill against the **training fold's** class frequencies (`class_prior` on train labels), log loss, per-class reliability on equal-mass bins, classwise binned ECE behind an `ece=` estimator argument (the debiased estimator is #132's), MDA permutation importance by log loss on an out-of-sample slice (never MDI). | [built #130]; debiased ECE [planned #132] |
| Artifact | One weights file, format `m04.lightgbm.v1`, canonical JSON: class order, features, feature schema, label version, training cutoff, training record, the full config, the temperature, and one entry per seed `{seed, sha256, model}` where `model` is LightGBM's native `model_to_string` text. The card's `weights_hash` pins the file; each booster's sha256 is checked before `lightgbm.Booster(model_str=...)` parses it. Never pickle or joblib. | [built #130] |
| Card | `ModelCard.training_cutoff` and `weights_hash`: `predict` refuses any snapshot with `as_of <= cutoff`, so a model is never scored in-sample. `label_version` and `feature_schema` (the transform version). `ModelCard.training` (`TrainingRecord`): dataset ref, config hash, seeds, library versions (lightgbm, numpy) and the code commit, passed in because a `git archive` has no `.git`. The cutoff is the latest `available_at` of every label read (train and validation). | cutoff and weights pin [built] #127; the other fields [built #130] |
| Trusted adapter | A reviewed module in an allowlisted package loads the weights only after checking them against the sha256 pinned in the registered card. One adapter serves many versions. `app.prediction.adapters.lightgbm_native` refuses a schema, label, feature list, class order, cutoff, seed list or config that differs from the card. `load_model(..., card=, weights=)` loads an adapter model on the live path (registered card, approval required); without them it still calls `build()`. | evaluation path [built] #127, see §10; live adapter load [built #130]; live wiring of a model provider [planned #133] |
| Evaluation load | `load_model` is point-in-time since #126, so a model registered today is refused for every historical `now`. An explicit **evaluation load** is hash-verified, needs no eligibility, and no live path accepts it. | [built] #127, see §10 |
| Where it runs | Colab CPU high-mem. Probed at 8 vCPU, 50 GB RAM, 226 GB disk and 0.26 CU/h. The code arrives as a `git archive` of a pinned commit, the VM holds no secrets, and only the artifact and manifest come back. Locally they are registered only after a hash check. Every VM is stopped after use. | [planned #134] |

## 5. Evaluation tiers (always reported separately)

1. **Model metrics per fold:**
   - multiclass Brier skill;
   - ECE;
   - direction accuracy with a block-bootstrap 95% CI over trading days;
   - precision at high confidence, with coverage;
   - per-regime buckets, each with `n`; thin buckets get no verdict. [planned #130/#132]
2. **System backtest.** `DecisionLoop`, M06 and `FakeBroker` run over stored bars on a fresh DB per run. The clock follows bar availability. The model's forecast reaches M05 through a **model-backed evidence provider** (`EvaluationEvidence`); the snapshot is passed in, not rebuilt ([built] #127). Reported:
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
| 2 | **Uniqueness weights and time decay.** Pre-register whether claim metrics are weighted. Time decay is a hyperparameter inside the budget. | #130 | uniqueness weights [built #130]; time decay [planned] |
| 3 | **Horizon M15–H1 as configs.** Watch label overlap. | #130/#132 | [planned] |
| 4 | **Volatility-model WAIT gate.** Block-only, following the `NEWS_VETO` pattern. New evidence contract. Baseline is persistence plus the M02 regime rule. | #139 | [planned] |
| 5 | **Meta-labeling arm.** Predictor-port contract change. Needs an evaluation path for strategy eligibility over history (the same seam as #127). The dataset pins the strategy rule hash. | #140 | [planned] |
| 6 | **Cross-market data-only symbols** (XAGUSD and a USD proxy). Schema bump, never tradable, one session schedule per symbol. Admitted only with a live source. Measure the skew between the training feed and the serving feed. | #141 | [planned] |
| 7 | Yields, VIX and the pre-event countdown. Need point-in-time sources. | after #41 / OD-14 | [blocked] |
| 8 | Chart+News fusion and regime-specific ensembles, from Shadow data. | after #17, #41 | [blocked] |
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

## 10. Design decisions from the tracer (#127)

All four were open before #127; each is now **[built]** in #127 (branch `feat/127-training-tracer`).

**1. Evaluation load path [built #127].**
- `load_for_evaluation(loader, registry, card=, weights=, module=)` in `prediction/engine.py`. It reads no eligibility. It refuses:
  - a card with no training cutoff;
  - a card that is not the registered one;
  - adapter bytes that fail the hash check (`ArtifactLoader.load_for_research`);
  - weights the adapter rejects.
- It returns an **`EvaluationPredictor`**. This is a distinct type, so the `LoadedPredictor` gate is unchanged. Only `predict_for_evaluation` accepts it.
- **Live paths refuse it:**
  - `predict` refuses it with its existing isinstance gate.
  - `MarketRecorder` refuses any non-`LoadedPredictor` at construction, whatever the provenance. It used to drop such a model silently in `_predict`.
  - `DecisionLoop` refuses `EvaluationEvidence` at construction. Its evidence signature `(now)` also differs from replay's `(now, snapshot)`, so mypy rejects the mix.
- **Limit:** a hand-written lambda that wraps an evaluation predictor defeats the runtime check. Only review catches that.

**2. Adapter and weights [built #127].**
- One reviewed adapter module per model family lives in the allowlisted package `app.prediction.adapters`. Today that is `barrier_softmax` (one three-class softmax head scoring the triple-barrier label) and `lightgbm_native` (#130, §4).
- It defines `build(card, weights: bytes) -> Predictor`. `load_for_evaluation` checks the weights with `verified_weights` (in `prediction/card.py`) before calling `build`, and the adapter checks again before it parses anything. It never uses pickle, and JSON numbers parse as Decimals.
- **Three pins per version, all immutable:**
  - the adapter source is the registered `artifact_hash`, the same for many versions;
  - the card's `weights_hash` pins the weights file;
  - the card itself is pinned through its manifest: `ModelCard.manifest()` adds `CARD_PIN` + `<sha256 of the canonical card JSON>` to `StrategyManifest.dependencies` whenever `weights_hash` is set. A card is no longer inside the hashed module, so an edited cutoff or `calibrated` claim is refused as "not the registered card".
- No contract schema changed: `ModelCard` is not in `docs/contracts`, and `gen_schemas.py` produced no diff.

**3. Evidence provider input [built #127].**
- Replay's `EvidenceProvider` is `(now, snapshot) -> Evidence`. The step passes the snapshot it already built, with the session and techniques it used. `EvaluationEvidence(model)` is the model-backed provider; a refused prediction gives no forecast, so M05 waits.
- `DecisionLoop` keeps `(now)` for now: `journal_evidence` has about 20 call sites. Switch it when a live model provider is wired (#131/#133).

**4. Tracer fixture [built #127]** (`tests/integration/test_training_tracer.py`, `tests/support/tracer.py`).
- **Fixture:** seeded synthetic M5 bars on `XAUUSD_NY_1700` with trend legs of 18-36 h. M15/H1 are resampled from them. The import link is stubbed until #128.
- **Chain:** record (BACKFILL, `session=`) → label → export → export M04 transforms and `m07.labels.tb.v2` → train a three-class softmax on two transforms (`H1/M15.close_sma_atr`) on train → temperature-scale on validation → card (`label_version`) + JSON weights → `propose_candidate` → `load_for_evaluation` → `replay(session=)` on the holdout with real `trend_following` → `EvidenceStore`.
- **Asserts:**
  - H1/M15 features set on the test fold and after both reopens;
  - every row labelled or marked unlabellable exactly at a closure;
  - at least one calibrated ENTER;
  - `NOT_READY` with `POLICY_UNCERTIFIED`.
- **Negative control:** with `session=None`, H1 ADX and M15 structure never come back, so the feature assertion fails.
- **CI-time deviations from "7+ days of M5":**
  - The library is the default one cut to the techniques the chain reads (`tracer.TECHNIQUES`): same ids and values.
  - M5 is stored only from the train split on, while M15/H1 history starts a week earlier.
  - Measured locally (Windows): recording 2,760 M5 bars (about 10 trading days) with the full library took 42 s for the recorder alone; the tracer takes about 16 s (about 7 s of it is `transform_from_store`, which reads 600 M5 bars per row and per replay step) and the suite grows from about 49 s to about 69 s. CI runs only on pull requests, so no CI time exists yet.
- **After #129 (merged at `3b10ae3`, `m07.labels.tb.v2`):** EV is priced only from a forecast's class probabilities of the current `LABEL_VERSION`. The evaluation path applies the same rules as the live one (a card on another label is refused at load; a labelled model's forecast carries its class triple), and `EvaluationEvidence` feeds a transform model `transform_from_store`, as the recorder and the export do (train/serve parity). The tracer's ENTER fires under that EV with no threshold or fixture change, and every ENTER has a positive EV.
- **What the fixture does not exercise:** H1 MACD (not in the cut library), M5 history beyond about 2.5 trading days, and on the negative control only about 4 hours of M5 around the daily break (run both with and without the session on that window).
- **After #130 (branch `feat/130-lightgbm-trainer`):** the tracer runs once per adapter (`barrier_softmax`, `lightgbm_native`) on the same recorded and exported data and the same fold plan. The train split now ends at Monday 14:00 UTC instead of 06:00: it held a single falling trend leg, which trees cannot extrapolate from, so it now runs into the first hours of the rising leg; validation (the calibration slice) is the rest of that leg, and the test fold is unchanged. The softmax passes unchanged on the new plan. The LightGBM config is tiny (4 leaves, 20 rows per leaf, 5 seeds). Both chains reach a calibrated ENTER with positive EV. Measured locally (Windows): the tracer file takes about 20 s of fixture setup (about 16 s for record, export and the softmax chain, about 4 s for the LightGBM chain); the full suite about 78 s.
