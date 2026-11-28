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
| Historical bars | Import M1 bars and resample to M5/M15/H1, with spread (if the vendor has it) and a **data-source** tag separate from LIVE/BACKFILL. Validation matches live bars, and chunks are resumable. Sources: `CsvM1Source` (generic format) and `FirstRateGcSource`. | [planned #128] |
| Training source | **Dukascopy XAUUSD M1** (spot; bid OHLC, spread = ask close − bid close), data source `dukascopy-xauusd`, schedule `XAUUSD_NY_1700`, by **owner decision 2026-11-28** (free, public data may be used; HistData is the fallback). `DukascopyFetcher` fetches one day file at a time, with a pause, backoff and a gitignored cache (`data/historical/dukascopy/`); `scripts/dukascopy_import.py` checks the price scale on a reference day, then fetches and imports. Dukascopy had been rejected on its terms; that reading is kept as history. FirstRate GC (`firstrate-gc`) stays as a reference source. | [planned #128]; the fetch has not run yet |
| Sealed windows | Protocol v1 (#132) length rule, fixed 2026-11-28 (certified by the agent under owner delegation): holdout `[2024-05-28, 2026-11-28)` (30 months), unread slice `[2023-11-28, 2024-05-28)` UTC; development data is 2008-01 → 2023-11. `require_unsealed` makes `export_dataset` and the `MarketRecorder` backfill refuse a real-source window ending after 2023-11-28 unless #132's `open_holdout` token is passed (and even then never the unread slice); the cost log never reads past it. TODO #131: the system backtest entry calls the same guard. | [planned #128] |
| Cost log | `era_costs`: per calendar year, median close spread and median spread/ATR_M15, reported beside `STRESSED_COST`. Information only; never changes the V2 bound. | [planned #128] |
| No-trade minutes | FirstRate omits minutes with no trade. A run of ≤ 15 open-session minutes between two trades is filled flat at the previous close, volume 0; a bar made only of such minutes is flagged `NO_TRADE`, not `GAP`, and is labelled normally. A longer run is a real gap. The import report lists every filled run's length. | [planned #128]; limit [PROPOSED] |
| Rolls | Only the **ratio-adjusted** continuous series is read (multiplicative back-adjustment: scale-free features are unchanged). Roll-crossing rows are kept. Roll dates are recorded per dataset version in `candle_rolls` from FirstRate's contract listing. | [planned #128] |
| Features | `build_feature_snapshot`, schema `m02.features.v2`, point-in-time, session-aware. | [built] #31, #124 |
| Per-bar dataset | `MarketRecorder` records one snapshot per closed bar (LIVE or BACKFILL) into `learning_snapshots`, in one transaction per pass. | [built] #108 |
| Outcomes | `BarLog.label_due` measures the move on the finest timeframe with bars closing at both ends. A row that cannot be labelled is marked once, point-in-time (`learning_unlabellable`). | [built] #108, #125 |
| Recorded predictions | Candidate and champion predictions per bar go to `learning_predictions`. They are learning data only and never trade. | [built] #109 |
| Export | `export_dataset` writes sorted, deterministic JSONL plus a hashed manifest. News is joined at `as_of` and leaking rows are purged. | [built] #110. Splitter folds, embargo and `available_at <= split.end`: [planned #128] |

**Rules for the data layer:**
- Data is point-in-time everywhere: a row uses only what was available at its `as_of`.
- Raw vendor data is never committed.
- FirstRate GC rules (planned until the data arrives; pre-declared in `docs/research/2026-11-28-xauusd-data-sources.md` §5):
  - Prices are futures **trades**, not bid, and have no spread: labels use the locked spread buffer (#129).
  - Adjusted prices are not tradable levels. Never compare them with the broker's spot prices; use scale-free features only.
  - Ratio-adjusted history is rescaled at every new roll, so a later download is a **new dataset version**, never an append.
  - Volume is contracts traded, not tick volume: normalise or exclude it per data source (#129).
  - No holiday calendar: CME holidays and early closes are reported as real gaps.
  - Before the first training run, check the import report (no bars in closed time, the unexpected gaps, the filled-run lengths) and the raw minute gaps against the 15-minute limit.
  - Price-unit quantities (the raw outcome `move`, a fixed spread or slippage buffer) are scaled by the cumulative roll factor in older history: express them in ATR or percent terms (#129).
- News features are only `news.assessment` rows recorded live. The LLM never re-interprets past news, because of look-ahead through its training cutoff.

## 3. Labels and features for training

| Item | Definition | Status |
|---|---|---|
| Triple-barrier label | TP at +k1·ATR and SL at −k2·ATR, measured on recording-timeframe bars. Time barrier of N bars. Bid prices, with the spread paid on long exits and short entries. A slippage buffer applies. A bar that touches both barriers counts as SL first. A missing spread uses a locked buffer. The output is UP, DOWN or FLAT, versioned by `label_version`. k1, k2, N and the buffer are [PROPOSED]. | [planned #129] |
| Stationary transforms | One M04 transform path with its own schema version. Training and the predictor adapter call the same code. Examples: `(close−sma)/atr`, Bollinger position, `macd/atr`, log returns, `spread/atr`, `sin`/`cos` of the UTC hour. `tick_volume` is excluded or normalised per source. | [planned #129] |
| Offline/online parity | Live model evaluation uses the same label version and transforms as training. The raw `move` stays as raw data. | [planned #129] |

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
| 0 | **EV and the label price the same event.** EV assumes a symmetric ±`TARGET_ATR`·ATR_M15 bet (`fusion.expected_value`). Triple-barrier barriers, the treatment of FLAT, the horizon and any meta-label P(win) must match it, or EV must change with them. | #129 | [planned] |
| 1 | **Session and liquidity features** from the gold bars: distance to the previous session's high and low, the London/NY volatility ratio, spread anomalies. | #129 | [planned] |
| 2 | **Uniqueness weights and time decay.** Pre-register whether claim metrics are weighted. Time decay is a hyperparameter inside the budget. | #130 | [planned] |
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

## 10. Open design decisions (resolve in #127)

- **Evaluation load path** for historical scoring: no eligibility needed, and never accepted by a live path.
- **Adapter and weights:** how `build` gets the card and the verified weight bytes, with one reviewed adapter for many versions.
- **Evidence provider input:** it receives the snapshot instead of rebuilding it.
- **Tracer fixture:** a daily break, a weekend, M5 recording and M15/H1 horizons. It asserts usable features, label coverage and at least one ENTER in the test fold. A negative control with `session=None` must fail. Readiness must be NOT_READY under the default policy.
