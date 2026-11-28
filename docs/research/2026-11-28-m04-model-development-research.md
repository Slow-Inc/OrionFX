# M04 model development: highest-value choices under a 20-config budget

- **Status:** SNAPSHOT (2026-11-28). Don't edit it; add a newer note when you revisit.
- **Provenance:** written by a research agent in a read-only session, then copied into the repo unchanged for #132 (only this header was added). Its remarks about Protocol v1 were made without seeing the v1 text.
- **Used by:** `2026-11-28-m04-protocol-v1.md` (draft), which adopts its F3/F4 corrections, its pipeline defaults (§9 A) and its config plan (§10), and resolves the forks it leaves open.

Research notes, 2026-11-28. Read-only task: no repo files were edited.

## Evidence legend

| Tag | Meaning |
|---|---|
| **[H]** | High evidence. A primary source, or maths that follows directly from it. |
| **[M]** | Medium evidence. A credible source, but a different market or setting; or strong practitioner consensus. |
| **[L]** | Low evidence. Thin or anecdotal sources, or my judgment. |
| **(nf)** | The URL was not fetched this session; the citation is from memory of the source. |

**Repo context read:**
- `docs/agents/training-loop.md`
- `docs/research/2026-11-28-m04-quant-upgrade-proposals.md`
- `docs/research/2026-11-28-m04-dataset-training-plan.md` (first 80 lines)
- `backend/app/decision/fusion.py` (`expected_value`)

I did **not** see the Protocol v1 text itself. Every remark below about what Protocol v1 allows is inferred from `training-loop.md` §6.

---

## 0. Load-bearing findings (read these first)

### F1. The EV formula and the forecast contract do not match the brief's decision rule. [H — read from code: `fusion.py:269-296`, `prediction/engine.py:128-185`, `core/enums.py:97`]

This refines backlog Pri 0 in `training-loop.md` §8a ("EV and the label price the same event"), which already notes that FLAT is missing from EV.

**1. The formula.** `fusion.expected_value` computes:

```
TARGET_ATR · ATR_M15 · size · (2p − 1) − cost      with TARGET_ATR = 1
```

Here `p` is `PredictionGenerated.probability`. `engine.predict` sets it to the **leader's raw score** from the predictor's `Mapping[Direction, Decimal]`. `Direction` = {UP, DOWN, NEUTRAL}.

With a 3-class model whose scores sum to 1:

```
2p − 1 = p_side − p_opp − p_neutral
```

So FLAT/NEUTRAL is priced as a **full −1·ATR loss**. The brief's rule, `p_side − p_opp − cost/ATR`, prices it at about 0.

**2. The contract.** The forecast event carries only the leader direction and its score; `p_opp` is discarded. And when **NEUTRAL is the argmax**, `f.direction` never matches a proposal's direction, so EV is `None` and M05 waits.

Example: (UP 0.40, DOWN 0.15, NEUTRAL 0.45) has an edge of 0.25, yet it can never trade. At an H1 horizon with ~20% FLAT this matters sometimes. At an M15 horizon with ~50–80% FLAT, it blocks most signals.

**Action:** M05 and M04 must agree on one formula and one contract before any config runs.
- **Recommended form:** `EV/ATR = k·(p_up − p_down) + p_flat·E[r_T | FLAT]/ATR − cost/ATR`. With symmetric barriers and no intraday drift, the FLAT term is ≈ 0.
- The forecast must then carry the full calibrated distribution, not just the leader.
- This changes M05's decision rule and the forecast contract, and may be owner-governed. **Flag this to main.**

### F2. Use symmetric barriers on **bid** prices with **no spread inside the barriers**. Put costs only in EV and the backtest. [M — design logic]

**Why bid, not mid.** Live candles are bid-only and store the max spread in the bar, so a mid price cannot be reproduced live. Offline/online label parity (#129) therefore requires bid.

Here is the case against labelling per direction with costs in the barriers:
- If the spread is paid inside the barriers, a long loss is not the same event as a short win.
- Long-SL sits at `mid0 − ATR + (s0+s)/2`. Short-TP sits at `mid0 − ATR − (s0+s)/2`.
- Long-win and short-win stay mutually exclusive, but they are **not exhaustive**: a "both lose" band appears.
- So `p_side − p_opp` is no longer the long payoff.

Using Dukascopy spreads in the label also makes the label itself **source-dependent** (train/serve skew).

Symmetric ±k·ATR barriers on bid prices, with no spread inside them, fix all of this (the spread cancels to first order when both ends are measured on bid):
- They give a clean partition: UP, DOWN, FLAT.
- They make `p_up − p_down` the exact payoff driver.
- They keep cost modelling in one place: M05 EV plus the system backtest, where it can be stressed.

### F3. Barrier and horizon geometry decides the class balance. Most of it can be set without spending configs. [H for the maths, M for applying it to gold]

Model the price as driftless Brownian motion with per-M15-bar std σ. ATR(M15) ≈ c·σ, where c ≈ 1.6 is the Parkinson expected range for continuous paths, and lower with discrete monitoring or gaps. The probability that neither ±k·ATR barrier is touched within T M15-bars is:

```
P(FLAT) = (4/π) Σ_n (−1)^n/(2n+1) · exp(−(2n+1)² π² T / (8 (k c)²))
```

| k (×ATR_M15) | c | T=1 (M15) | T=2 (30m) | T=4 (H1) | T=8 (2h) |
|---|---|---|---|---|---|
| 0.75 | 1.6 | 0.54 | 0.23 | 0.04 | 0.00 |
| **1.0** | 1.6 | **0.78** | 0.49 | **0.19** | 0.03 |
| 1.0 | 1.2 | 0.54 | 0.23 | 0.04 | 0.00 |
| 1.5 | 1.6 | 0.97 | 0.82 | 0.54 | 0.23 |

**Implication:** with `TARGET_ATR = 1`, an **M15 time barrier gives a mostly-FLAT label**. It carries little directional information per sample, and the FLAT class swamps the calibration. **H1 (4 M15 bars) is the natural horizon for k = 1.** The expected first-passage time is (k·c)² ≈ 2.5 M15 bars ≈ 40 min.

**Break-even.** The required edge is `p_up − p_down > cost/(k·ATR)`. Raising k lowers the required edge linearly. But it needs N to grow roughly as k² to keep the FLAT share fixed, which raises label overlap, which lowers the effective sample size.

**Do not guess cost/ATR.** Measure it as a label-blind diagnostic:
- the distribution of `broker_spread_profile(hour-of-week) / ATR_M15` by year and by session;
- the same for Dukascopy.

Gold rose 4–5×. ATR in dollars rose with it, while spreads in dollars rose less. So cost/ATR is probably much **higher in 2016–2019 than in 2024–2026** [L — unmeasured]. That makes the early era a different cost regime, not just a different price level.

### F4. The ECE ≤ 0.02 gate can fail on sampling noise alone. [H — maths; Roelofs et al. 2022]

For a perfectly calibrated model, with B equal-mass bins, n *effective* samples and per-bin p ≈ 0.4:

```
E[ECE_bin] ≈ 0.8 · sqrt(0.24 · B / n)
```

With B = 15:

| n_eff | Noise-only ECE |
|---|---|
| 2,000 | 0.034 |
| 5,000 | 0.022 |
| 10,000 | 0.015 |
| 20,000 | 0.011 |
| 50,000 | 0.007 |

Overlapping H1 labels sampled every M5 bar give **n_eff ≈ n/12**.

Roelofs et al. show that the binned ECE is biased upward, and most strongly for well-calibrated models. They recommend the debiased estimator (Bröcker 2012; Ferro & Fricker 2012) or ECE_sweep (equal-mass bins, monotone).

**A worked example.** With M15 sampling and an H1 horizon (concurrency 4), one 6-month test fold has about 12k rows, which is n_eff ≈ 3k. The noise-only ECE is then ≈ 0.028. So **a per-fold ECE ≤ 0.02 gate fails for a perfectly calibrated model**. Pooled across 12 folds, n_eff ≈ 36k and the noise-only ECE is ≈ 0.008. The protocol must state where the ECE gate is applied: pooled out-of-sample, or per fold with a debiased estimator.

**Action (zero-config):**
- Pre-register the ECE estimator: debiased or sweep, equal-mass bins, and per-class or top-label stated explicitly.
- Pre-register the minimum n_eff per evaluation slice.
- Run the protocol's power check (rule 3) on ECE as well as on expectancy.

### F5. The binding gate is probably stressed-cost **system expectancy**, not Brier. [L/M — judgment]

The real exits (Emergency SL, the USD cycle target, basket trailing) are not the label's barriers. So the label is a *proxy*. Configs that only nudge Brier skill are low value. Rank configs by how much they can move system expectancy at stressed cost. That favours:
- label and horizon geometry;
- regime/cost robustness (time decay, ATR-normalisation);
- session features, which concentrate trades where cost/ATR is low and moves are directional.

### F6. Near-duplicate configs spend budget without adding information. [L — my judgment; the DSR formula is from Bailey & López de Prado 2014 (nf)]

DSR's expected maximum Sharpe under the null is `sqrt(V[SR]) · ((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne)))`:

| N | Factor |
|---|---|
| 5 | 1.19 |
| 10 | 1.58 |
| 20 | 1.90 |
| 40 | 2.19 |

The penalty grows slowly in N. For near-duplicates the DSR effect is ambiguous: N rises, but V[SR] shrinks. So the real argument is **opportunity cost under a raw-N cap of 20**: a seed or library variant tests almost no new hypothesis and spends a slot. Seed averaging belongs *inside* a config. A library swap rarely earns a slot (see §4).

---

## 1. Data design

### 1.1 Bars

**Use time bars (M1 → M5/M15/H1).** [M]

AFML ch. 2 argues that tick, volume and dollar bars have better statistical properties (closer to IID normal, less heteroscedastic). That evidence is from futures with real traded volume (nf: López de Prado 2018, AFML ch. 2).

That benefit does not carry over here, for four reasons:
- Spot XAUUSD has no consolidated volume. Dukascopy and broker tick volume count quote updates for one venue, and the counts differ by source.
- With only M1 bars, you can only build tick-count bars at 1-minute resolution.
- The live `CandleEngine` builds time bars, so information-driven bars would add train/serve parity risk.
- The decision loop runs on bar close.

**Get most of the benefit by vol-normalising instead:**
- returns/ATR;
- an ATR percentile;
- a "time in activity units" feature, such as the cumulative tick count or range since the session open, divided by its trailing median. Normalise it per source.

### 1.2 Multi-timeframe alignment

**Join on bar *close* (availability) time with a backward as-of merge.** [H — standard; the repo already states the principle in `available_at`]

The classic leak works like this. Pandas `resample('1h', label='left')` stamps 10:00 on the bar covering 10:00–10:59. If an M5 row at 10:35 is then joined on the open timestamp, it sees an H1 high/low/close from the future (up to 25 minutes ahead, and the full bar's close).

**Rules:**
- Key every higher-timeframe (HTF) bar by `close_time = open_time + duration`.
- Use `merge_asof(direction='backward')` on `close_time ≤ decision_time`.
- If a "current partial H1" feature is wanted, compute it from M1 bars up to t only, and add it as a separate feature.

**Unit test:** for every row, `max(feature source timestamp) ≤ as_of`. Also run a shuffled-future canary: append `close[t+1]` as a feature. A correct pipeline must show a huge jump in skill, which proves the test can detect leaks. Then remove it.

### 1.3 Gaps, sessions, rollover

[M]

- **Daily break (~17:00–18:00 NY) and weekends.** Count horizons in *trading* bars, using the repo's `SessionSchedule`. **Flag or drop samples whose label window crosses the daily break or the weekend.** Their outcome is dominated by gap risk the model cannot see, and the live system's Emergency SL does not fill at the barrier either.
- **Rollover window (17:00 NY ± ~15 min).** Spreads blow out here. Exclude these samples from training and from the trading mask, or mark them with a feature. Do not let them define labels.
- **Missing M1 bars.** Distinguish *scheduled* gaps (the session schedule) from *data* gaps. Drop labels whose window contains a data gap longer than a few minutes.

### 1.4 The era shift from 4–5× price growth

[H — mechanics of trees]

**Trees cannot extrapolate.** A split threshold learned on 2016–2019 dollar values (ATR in $, range in $, spread in $) puts every 2025 value into the outermost leaf. So **every feature must be scale-free**: divided by ATR or by price, a log return, a ratio, or a trailing rank or percentile.

The same rule applies to barriers: k·ATR, never fixed dollars. The repo plan already does this. Keep it absolute.

### 1.5 Normalisation and leakage

[H]

**Global scaling is pointless for GBDT.** It is invariant to monotone transforms of a single feature. Its only effect is leak risk. So **don't z-score globally**.

Rolling z-scores and percentiles are useful, because they are adaptive and regime-relative. They must be:
- trailing only (window ends at t, using closed bars only);
- `min_periods` = the full window;
- computed **before** any train/test split, on the continuous series. That is safe because they are causal, and it avoids a cold start at every fold.

The leaky variants to avoid:
- a centred window;
- `fillna(method='bfill')`;
- a scaler fitted on the full sample;
- expanding statistics computed per fold on the concatenated train+test.

### 1.6 Spread modelling (Dukascopy ECN vs the broker)

[M]

- **Labels:** bid prices, symmetric barriers, no spread inside them (F2).
- **Cost model in EV and the backtest:** use `max(broker hour-of-week spread profile, Dukascopy spread)` plus a slippage buffer. Stress it ×1.5 and ×2, as the protocol requires.
  - Build the broker profile from MT5 Demo ticks once #17 unblocks.
  - Until then, use a locked conservative constant per session, documented as [PROPOSED].
- **Spread as a model feature is a train/serve skew.**
  - Dukascopy M1 bid/ask gives roughly a *close-to-close* spread.
  - The live candle stores the *max spread in the bar*.
  - Broker spreads also have a different level and a different intraday shape.
  - Either drop spread features from V1, or use a **per-source self-normalised** form: `spread_t / trailing median spread of the same source, same hour-of-week`. That captures "spread anomaly" without the level difference, and it should be computed identically live.
  - Record the feature-vector skew check (protocol rule 6) on overlapping Dukascopy and broker windows.

---

## 2. Dataset construction

### 2.1 Sampling: every bar vs a CUSUM event filter

[M]

AFML ch. 2.5 introduces the CUSUM filter and ch. 3 triple-barrier on events (nf).

Event sampling cuts overlap and concentrates training on informative moments. **But the model must be calibrated on the distribution it is queried on.** If M05 asks for a forecast on every M5 bar, a CUSUM-trained model is miscalibrated off-event.

**Recommendation:**
- **Baseline:** sample at every **M15 close**. That gives 4× less overlap than every M5 bar at an H1 horizon and matches the label's ATR timeframe.
  - It keeps the query distribution equal to the training distribution **only if M05 queries at M15 closes**. The repo records a snapshot per closed bar, and the tracer uses M5 recording.
  - If M05 queries every M5 bar, either sample at M5 with uniqueness weights (concurrency 12) or gate M05 queries to M15 closes.
  - The n_eff figures in F4 and §2.5 assume M15 sampling. At M5 sampling, rows triple but n_eff stays about the same.
- **CUSUM only as a config** where M05 also gates queries on the same CUSUM event, so that query = train distribution. That makes it a system change, not just a model change. **It ranks low.**

### 2.2 Uniqueness weights and sequential bootstrap

[M — AFML ch. 4 (nf)]

- Weight each sample by its **average uniqueness**: the mean of 1/concurrency over its label span. At an H1 horizon with M15 sampling, concurrency is ≤ 4.
- **Sequential bootstrap** is for bagging-type learners (random forests). For GBDT it is impractical and unnecessary. Use uniqueness as `sample_weight` and keep `bagging_fraction` modest.
- **Make uniqueness weighting a base-pipeline default, not a config.** It is a variance-reduction correction with no hyperparameter. Pre-register whether claim metrics are uniqueness-weighted; the repo plan already notes this.

### 2.3 Class balance

[M]

- **Do not rebalance:** no SMOTE, and no `is_unbalance` or class weights. Reweighting distorts the probabilities that EV needs. The FLAT share is controlled by geometry (F3).
- UP/DOWN should be close to 50/50 by symmetry. Report the per-fold priors. The Brier-skill baseline already uses training-fold priors.

### 2.4 Horizon and barrier width

See F3. **Pick (k, N) by a label-blind, pre-registered rule on the debug window:**
- **k = `TARGET_ATR` = 1.** Changing it changes EV and M05.
- **N** = the smallest horizon in M15 bars for which FLAT ≤ ~25%. The Brownian table predicts **N ≈ 4 (H1)**.
- Measure the actual FLAT share. Gold has intraday seasonality and fat tails, so it will differ from the table.

There is a **second, better anchor**. In the first system backtest of the baseline, measure the **realised holding-time distribution** under the real exits (basket trailing and the cycle target). If positions typically live 2–4 h, an H1 label predicts the wrong thing. That argues for N = 8 with k = 1.5, and therefore a change to `TARGET_ATR`. [L — unmeasured]

### 2.5 Fold sizes

[M]

At M15 sampling, gold trades ~23 h × 4 ≈ 92 samples/day, or ~24k/year.

**Recommended walk-forward layout:**

| Block | Length |
|---|---|
| Training | expanding, from 2016 (≥ 3 years at the first fold) |
| Purge gap | ≥ 1 label horizon |
| Calibration slice | 3 months (~5.5k samples, ~1.4k n_eff at H1 overlap… **too small for isotonic, fine for temperature**) |
| Purge gap | ≥ 1 horizon |
| Test | 6 months |
| Step | 6 months |

From 2019 to ~2025 that gives ≈ 12 test folds, plus a **sealed holdout of ≥ 12 months**.

**Power check for the holdout.** The minimum track-record length to reject SR ≤ 0 at 95% is:

```
MinTRL ≈ 1 + (1 − γ3·SR + (γ4−1)/4·SR²) · (1.645/SR)²
```

For a daily SR of 0.1 (≈ 1.6 annualised) that is ≈ 270 trading days. **A holdout shorter than about a year cannot pass a meaningful expectancy or Sharpe gate.** [H — Bailey & López de Prado 2012, the Sharpe-ratio efficient frontier (nf)]

### 2.6 Handling the 2020–2026 regime shift

[M]

- **Time-decay weights** (AFML ch. 4.5): linear decay to weight c for the oldest sample. Make it **one config** with c = 0.5, against the baseline c = 1.
- **Era-stratified reporting is free and mandatory.** Report the per-fold metrics grouped by era (2019–21, 2022–23, 2024–26) and by vol tercile. That is evaluation, not selection, so it costs no configs.
- **Do not drop 2016–2019.** The ATR-normalised label and features make those years informative. Their cost regime differs, but that is a backtest matter, not a training one.

---

## 3. Features

### 3.1 What carries out-of-sample signal for gold intraday

| Family | Evidence it predicts **volatility** | Evidence it predicts **direction** | Notes |
|---|---|---|---|
| **Vol regime**: ATR percentile, realised-vol ratios across M5/M15/H1/D, HAR-style components | **[H]** Vol clusters strongly and is forecastable (Corsi 2009, HAR-RV, nf). | [L] | It conditions *how often* barriers are hit and *when* cost/ATR is low. It helps the FLAT split and, through interactions, the direction. |
| **Session structure**: time-of-day sin/cos, session dummies (Asia/London/NY/overlap), minutes to the LBMA auctions (10:30 and 15:00 London) and COMEX open, day-of-week, distance to the prior session's high/low, Asian range | **[H]** Intraday seasonality in gold volatility, and announcement effects at 08:30 ET (Cai, Cheung & Wong 2001; Smales 2015). | **[L/M]** Intraday half-hour return predictability exists in gold ETFs, but its pattern is market-specific and stronger on high-vol and jump days (Xu, Bouri, Saeed & Wen 2020). Equity intraday momentum: Gao, Han, Li & Zhou 2018, R² ≈ 1.6%. | **Best prior per config spent.** It is cheap and it comes from the gold bars alone. |
| **Momentum vs mean-reversion at several horizons**: returns/ATR over 1, 4, 12, 48 bars and 1 day; position in the trailing range; `(close − SMA)/ATR` | [M] | [L] The sign and strength are regime-dependent. Trees can learn the interactions with vol and session. | Keep 4–6 horizons, not 20. |
| **M1 microstructure proxies**: wick ratios (upper and lower wick / range), close location value, range/ATR, Corwin–Schultz high-low spread estimate, tick-count ratio (per source) | [M] | [L] | Noisy. Tick-count features are source-dependent, so self-normalise them. |
| **Calendar**: month, FOMC/NFP days | [M for vol] | [L] | No point-in-time calendar yet (#41). Day-of-week only. |
| **Cross-market**: XAGUSD return/ATR, the gold/silver ratio z-score, EURUSD as a USD proxy | [M] | [L/M] | The USD factor is gold's dominant macro driver, but contemporaneous correlation is not a lead. It needs a live source (#141). Schedule it late. |

**Honest baseline expectation.** I found **no high-quality, peer-reviewed study** showing an exploitable intraday XAUUSD directional edge after realistic costs with proper walk-forward. What exists is MQL5 blogs, theses and low-tier journals. **[H for the absence; L for any specific edge size]** Treat "the model has no usable edge" as a live outcome, which is what the Protocol v1 stop rule is for.

### 3.2 Feature importance done right

[M/H — AFML ch. 8; MLAM ch. 6 (nf)]

- **Do not use MDI** (gain or split importance). It is in-sample, biased toward high-cardinality and continuous features, and spread across correlated substitutes.
- **Use MDA**: permutation importance **on the purged out-of-fold slices**, scored by log-loss.
- **Use clustered MDA** (MLAM ch. 6). Cluster correlated features and permute whole clusters, so substitutes don't mask each other.
- **SFI** (single-feature importance) is immune to substitution effects. It is cheap with small trees, and useful as a sanity check.
- **Stability criterion:** keep a feature cluster only if its MDA is > 0 in ≥ 70% of the training folds. That figure is my suggestion, [L]. An arXiv MNQ study found feature importance unstable across walk-forward folds, which supports requiring stability.
- **Selection must be nested.** Compute importance using **training-period folds only** for each outer fold. Choosing features on the outer test folds is selection, and it must count toward N.

### 3.3 How many features?

[L/M]

There is no hard rule. With ~25k effective samples per year, SNR near zero, and strong regularisation, GBDT tolerates 50+ features. But every extra noise feature adds split opportunities and variance.

**Practical recommendation:** **25–40 scale-free features in the baseline**, grouped into ~8 clusters. One pruning config (§3.2) tests going down to ~15.

---

## 4. Model and training

### 4.1 LightGBM settings for noisy financial targets

[H for the docs' list; M for the values]

The LightGBM "Deal with over-fitting" list, verified in the docs this session:
- smaller `max_bin`;
- smaller `num_leaves`;
- larger `min_data_in_leaf` and `min_sum_hessian_in_leaf`;
- bagging (`bagging_fraction` + `bagging_freq`);
- `feature_fraction`;
- `lambda_l1` / `lambda_l2`;
- `min_gain_to_split`;
- `max_depth`;
- `extra_trees`;
- `path_smooth`.

**Pre-registered baseline:**

| Parameter | Value |
|---|---|
| `objective` | `multiclass` (softmax) |
| `num_leaves` | 15 |
| `max_depth` | 4 |
| `min_data_in_leaf` | ~1–2% of the training rows (≈ 500–2000) |
| `learning_rate` | 0.02 |
| `feature_fraction` | 0.7 |
| `bagging_fraction` | 0.7, with `bagging_freq` = 1 |
| `lambda_l2` | 10 |
| `max_bin` | 63 |
| `extra_trees` | false |
| `path_smooth` | 0 |

- **Early stopping on a purged inner validation slice**, the last 10–15% of training time, with log-loss as the metric. Then refit on the full training set with `best_iter × 1.1`, or keep the early-stopped model. Fix the choice once.
- **Monotone constraints:** skip them. No feature has a known monotone relation to direction. [L]
- **Seeds:** average **5 seeds inside every config**. This is variance reduction, not a trial.
- **A/A run:** the same config with 5 different seeds. It gives the noise floor (protocol rule 5).

**Hyperparameter tuning is the biggest budget sink.** Do **not** grid-search. Allow at most **one** "regularisation level" config: stronger, with `num_leaves` 7, `min_data` ×2 and `extra_trees` on. Alternatively, pre-register a fixed nested tuning procedure (for example a 20-trial Optuna run inside each training fold) and **count the whole procedure as one config**. Its inner trials never see outer test data.

### 4.2 Multiclass vs one-vs-rest, and the label form

[M]

- **Use softmax `multiclass`, not `multiclassova`.** Softmax gives probabilities that sum to 1 and a coherent `p_up − p_down`. OvA gives three independent sigmoids that need renormalising.
- **FLAT-aware 3-class (recommended)** vs a decomposition into `P(hit)` × `P(up | hit)`:
  - The decomposition is equivalent in information: EV = k·P(hit)·(2q − 1) − cost.
  - It can help when hit-probability (vol) and direction have very different signal strengths, which is likely: vol is predictable, direction barely.
  - It costs two calibrations and a contract change. **Worth one config, at mid priority.**
- **A pure "upper vs lower first" binary** (dropping FLAT samples) conditions on information unknown at decision time unless P(hit) is modelled. **Do not use it alone.**

### 4.3 Ensembling, and XGBoost or CatBoost

- **Seed ensembling:** inside each config, free (see above). [M]
- **XGBoost vs LightGBM:** same family, same inductive bias. Expect differences within seed noise. [M]
  - McElfresh et al. 2023 (176 datasets) found light tuning often matters more than the algorithm. I did not get per-library numbers.
  - Grinsztajn et al. 2022 supports GBDTs on tabular data generally.
  - **Do not spend a config on it** unless LightGBM shows a pathology.
- **CatBoost:** ordered boosting and symmetric (oblivious) trees are strong built-in regularisers. That is plausibly useful on noisy targets. **[L]** It gets one late, optional config at most. I claim no gain size.
- **Ensembling different configs** (for example the top two arms averaged): one late config. It is usually a small Brier gain. [L/M — Kaggle consensus, not verified this session]

---

## 5. Calibration

### 5.1 Method

[H for the source findings; M for the multiclass application]

- **Niculescu-Mizil & Caruana 2005:** boosted trees produce sigmoid-shaped distortion. Platt scaling fixes it, and isotonic beats Platt only with larger calibration sets. Their study is for AdaBoost-style boosting; modern log-loss GBDT is closer to calibrated. (I could not fetch the full text; this summary is from memory and the general literature.)
- **Guo et al. 2017:** temperature scaling, a single-parameter multiclass Platt, is "surprisingly effective" for multiclass. It preserves the ranking and is very low-variance. (Abstract verified.)
- **Kull et al. 2019:** Dirichlet calibration. Its full-matrix form needs a lot of data. (nf)
- **Alexandari et al. 2020:** bias-corrected temperature scaling (BCTS), temperature plus per-class bias. It handles label or prior shift, such as a FLAT share that drifts with regime. (nf)

**Recommendation:** use **temperature scaling as the base-pipeline default**. It is fixed a priori, so it is not a config. Allow **one config** for BCTS **or** isotonic-OvR plus renormalisation. Isotonic only if the calibration slice gives n_eff ≥ ~10k, which requires a longer calibration window (6 months) or calibrating on pooled out-of-fold predictions from the inner folds.

### 5.2 Size and placement of the calibration set

[M]

- Put the calibration set **immediately after training, purged, and immediately before the test**. That makes it the most recent data, which matters under drift.
- **Never calibrate on the test slice.** Do not use cross-fitted OOF predictions from folds that sit *after* the test period.
- **Alternative that uses data better:** fit the calibrator on **out-of-fold predictions from a purged inner walk-forward** within the training span, plus the recent slice. This gives far more calibration samples. It is a pre-registered choice, not a config.

### 5.3 Recalibration cadence

[L/M]

- Temperature has one parameter, so it can be refitted monthly on the last 3–6 months of resolved labels at low risk.
- **But** every recalibration is a model change under ADR 0001. Pre-register it as part of the model ("calibrator refit monthly on a trailing window, fixed rule"), so that it doesn't need a new protocol version each time.
- Monitor rolling Brier and debiased ECE on resolved forward labels. That is also the out-of-distribution trigger (§7.3).

### 5.4 Calibrate where the money is

[M]

M05 trades only when `p_up − p_down > cost/ATR`, which is in the tail. ECE is dominated by the bulk of the predictions. So also report the **reliability of `p_up − p_down` within the traded region**: the predicted edge vs the realised `(UP − DOWN)` frequency per edge decile, with block-bootstrap CIs. Overconfidence there directly inflates trade count and cuts expectancy.

---

## 6. Evaluation tooling

### 6.1 CPCV vs walk-forward for PBO

[H for the definitions; M for the choice]

- **PBO via CSCV** (Bailey, Borwein, López de Prado & Zhu 2017, J. Computational Finance) takes a **T × N matrix**: the per-period performance of N configs. It is model-free. PBO is the probability that the in-sample-best config ranks below the median out-of-sample.
  - You can build that matrix from the **walk-forward out-of-sample daily P&L** of each config. No CPCV is needed for PBO.
  - Use S = 16 time blocks, which gives C(16, 8) = 12,870 splits. This is cheap.
- **CPCV** (AFML ch. 12) gives a *distribution* of backtest paths per config. It is better for estimating the variance of the Sharpe ratio, but it trains models on data *after* the test segments. That needs an embargo of at least the feature lookback, and it costs φ× more training.
- **Recommendation for v1:** keep walk-forward, which is what is pre-registered, and compute PBO by CSCV on the walk-forward out-of-sample matrix. **CPCV is a Protocol v2 proposal, not a free fix.**

### 6.2 Deflated Sharpe inputs

[H — Bailey & López de Prado 2014, JPM (nf: SSRN 403'd)]

The inputs are:
- the observed SR at the **same frequency** as T;
- T, the number of returns;
- the skewness γ3 and kurtosis γ4 of the returns;
- **N**, the number of trials;
- **V[SR_n]**, the cross-trial variance of the Sharpe ratios, from all N configs' out-of-sample Sharpes.

**Use daily system P&L, not per-trade returns.** Basket trades are clustered and overlapping, so per-trade returns overstate T.

N should count **every config run on development data**, including aborted and bug-rerun ones. Rerunning after a bug fix arguably counts. Pre-register whether it does.

### 6.3 Effective number of trials

[M — López de Prado & Lewis 2019; MLAM ch. 8]

- Cluster the N configs' out-of-sample return series with ONC (optimal number of clusters on the correlation matrix). N_eff is the number of clusters. V[SR] is computed across the cluster representatives.
- **The conservative default for a binding gate is raw N = 20.** Report N_eff alongside it as information.
- Do not use N_eff to *loosen* a pre-registered gate unless Protocol v1 says so. **I have not seen the v1 text.**

---

## 7. Regime robustness and coverage

### 7.1 One model across regimes

[M]

**Order of preference:**
1. **Make the problem regime-invariant by construction.** Use ATR-scaled barriers, ATR- and price-scaled features, and rank features. This is the strongest single device, and it is free. It keeps class priors roughly stable across vol regimes.
2. **Use regime features** inside a single global model: vol percentile, trend strength, session. Trees learn the interactions. Free.
3. **Use time-decay sample weights.** One config.
4. **Gating or mixture-of-experts per regime.** This multiplies the parameters, thins the data per regime, and adds configs. **Defer it.** The repo plan already places it under ADV.

### 7.2 Keeping calibration stable under shift

[M]

- ATR-normalised labels are first-order.
- Second-order: temperature or BCTS on a **recent** calibration slice, and a monthly trailing refit (§5.3).
- Isotonic is the most prone to drift. Its step function memorises the calibration window.

### 7.3 Out-of-distribution detection and refusal

[M for the components; L for the thresholds]

All of these feed "M05 waits" (refusal when uncertain):
- **Hard range checks.** `ModelCard.feature_ranges` is already built. Set them to the training [0.5%, 99.5%] quantiles, and refuse when any feature falls outside.
- **Drift.** Per-feature PSI is already built. Add a multivariate score, for example the share of features beyond the 1% and 99% quantiles, or an isolation-forest score fitted on training data. **[L]**
- **Performance drift on resolved labels.** CUSUM or Page–Hinkley on the rolling Brier and log-loss against the fold's out-of-sample baseline. Breach → WAIT plus auto-rollback (ADR 0001). This is the most direct trigger, and H1 labels resolve within an hour.
- **Regime-bucket coverage.** If the current (vol tercile × session) bucket had thin or negative out-of-sample evidence, WAIT. This turns the protocol's regime-bucket check into a live gate.
- **Adaptive conformal inference** (Gibbs & Candès 2021) gives coverage guarantees under arbitrary drift. It is interesting for a later abstention rule. [L for usefulness here; nf]

---

## 8. Pitfalls and leakage specific to this setup

| Pitfall | Mechanism | Fix | Strength |
|---|---|---|---|
| Overlapping labels in train vs test | An H1 label at the last training row covers the first test hour. | Purge ≥ the horizon before *each* of: inner validation, calibration and test. The embargo matters mainly for CPCV, or when features use future-of-train data. | H |
| Both barriers touched in one bar | OHLC can't order the touches. | Resolve on **M1** (available). Where both are touched within one M1 bar, **drop or flag the row** and report the rate. A single 3-class label cannot be "worst case for both sides". (The repo's current V1 rule, SL first, is per direction.) | H |
| Entry price | The label starts at close[t], but the live entry happens after M05/M06 latency at the next tick. | Start the label at the **next M1 open** after the decision bar closes. Add latency slippage in the backtest. | M |
| ATR used for barriers | ATR at t includes bar t, which is fine if the decision is taken at the close of t. Using ATR at t+1 or later is a leak. | Freeze the barrier width at decision time. | H |
| Rolling statistics | Centred, bfilled or full-sample windows. | Trailing-only, plus a unit test (§1.5). | H |
| HTF join | Open-time stamps. | Join on close time with a backward as-of merge (§1.2). | H |
| Gaps and rollover inside the label window | The label measures gap risk. | Drop or flag those samples (§1.3). | M |
| Feature selection on test folds | Hidden selection. | Nested, training-only importance (§3.2). It counts toward N if not nested. | H |
| Survivorship of the best config | The winner's curse. | Single-use holdout; DSR at N = all trials; PBO; report best-of-N with N (already protocol rules 1 and 4). Rerun the **baseline** on the holdout as well, as the control. | H |
| Dukascopy spread vs the broker | Different level, shape and definition (close spread vs max-in-bar). | Bid-price labels with no spread inside the barriers; per-source self-normalised spread features, or none; the backtest costs = max(the two) × stress (§1.6). | M |
| Dukascopy vs broker prices | Small quote differences flip barrier touches near threshold. Broker server time is often GMT+2/+3. | Session features keyed on NY/UTC (the repo already does this). Measure the label agreement rate on overlapping windows when broker data arrives. | M |
| Tick volume | A different meaning per source. | Exclude it, or self-normalise it per source (the repo plan agrees). | H |
| The LLM-news feature | Not available historically. | Don't train V1 on it. A later model trained on the live-recorded period has a tiny sample, so treat it as a separate small model or veto. That is already the plan. | H |
| Weighted vs unweighted metrics | Metrics silently change meaning. | Pre-register (repo §8a item 2). | M |

---

## 9. The base pipeline: fix before spending any config (zero-budget items)

**Protocol status.** The brief says Protocol v1 is pre-registered. In the repo, `training-loop.md` §6 marks the protocol record **[planned #132]**, and a repo-wide grep found no hashed protocol file. The config space, fold plan, holdout window and gates live **inside** that record.

So the items below split into two groups:
- **(A) Pipeline defaults.** Code-level correctness, not selection choices. Free.
- **(B) Proposals to the protocol record.** The owner certifies them before the first run. If v1 is already frozen without them, they belong in v2.

**(A) Pipeline defaults (free):**

1. *(B, owner or M05)* **Reconcile EV and the forecast contract with the label (F1).** Use `EV/ATR = k(p_up − p_down) − cost/ATR`, or explicitly keep `2p − 1` and accept its conservatism. *Owner or M05 decision.*
2. **Bid-price symmetric ±1·ATR_M15 barriers (no spread inside)**, M1-resolved touches, label start at the next M1 open, and labels across a break or weekend dropped (F2, §8).
3. *(B)* **Horizon rule by FLAT share** (label-blind) on the debug window: expected N = 4 M15 bars (H1). Record the measured FLAT share by era.
4. **Close-time as-of joins**, trailing-only rolling statistics, the leak unit test, and the future-feature canary.
5. **Scale-free features only.** A baseline set of ~30 across vol regime, session/liquidity structure, multi-horizon momentum/mean-reversion and M1 bar shape. Tick volume and spread either self-normalised per source or excluded.
6. **Sample every M15 close**, with uniqueness weights on.
7. **Pre-registered LightGBM parameters** (§4.1), purged inner early stopping, and a 5-seed average.
8. **Temperature-scaling calibrator** on a purged recent slice, or on pooled inner out-of-fold predictions.
9. *(B)* **Debiased or sweep ECE**, with where it applies (pooled vs per fold), a minimum n_eff, and a power check for ECE and expectancy. A holdout ≥ 12 months. Fold sizes as in §2.5.
10. *(B for the locked stressed cost; A for the diagnostic)* **Cost diagnostics.** The cost/ATR distribution by era and session, for Dukascopy and the broker proxy. A locked stressed cost.
11. *(B)* **The A/A noise floor**: baseline × 5 alternate seeds. Pre-register it as *not a candidate*. *Protocol interpretation: the owner must confirm whether it counts toward the 20.*
12. **Non-ML controls** on the same folds: class priors; persistence ("always the direction of the last H1 return"); vol-only ("WAIT unless the ATR percentile is above x"). They benchmark Brier skill and system expectancy. They have no tuning, so they are not trials.

---

## 10. Config plan for the 20-config budget

**Governance** (follows `training-loop.md` §8a; the whole plan is a proposal for the protocol record, (B)):
- **Every arm is ablated against baseline B0 on the same folds and seeds.** An arm "wins" only if it beats B0 by more than the A/A noise floor.
- **Winners are merged only in explicit combination configs** (#8, #12), because one-at-a-time gains may not add up.
- **Primary selection metric (proposed):** system expectancy at stressed cost. Brier skill and ECE are hard gates, not the ranking metric.
- **Assumption:** this needs a **system backtest per config** on walk-forward out-of-sample data, which also supplies the CSCV/PBO matrix. The loop diagram places the system backtest in the claim stage. If it is too slow per config, use a **cheap proxy**: a vectorised replay of the M05 EV rule with stressed cost and fixed ±k·ATR exits on the out-of-sample predictions. Pre-register the proxy, and run the full system backtest only on the finalist.
- **DSR and PBO use N = all configs run.**
- **Order:** the highest expected impact on the binding gate per config first. Cheap, informative ones early. Data-blocked ones late.

| # | Config | What it tests | Expected impact | Confidence |
|---|---|---|---|---|
| 1 | **Baseline B0**: §9 pipeline, H1 horizon, k = 1, core features *without* the session/liquidity family | The reference point. Is there any skill at all? | — (mandatory) | — |
| 2 | **B0 + session/liquidity family**: distance to the prior session's high/low, Asian range/ATR, London/NY open flags, minutes to the LBMA auctions and the COMEX open, London/NY vol ratio | The best-evidenced gold-specific structure. It concentrates trades in low cost/ATR, high-activity windows. | **High** | M for vol and session structure; L for direction |
| 3 | **Horizon arm**: k = 1.5, N = 8 (2 h); `TARGET_ATR` changes to match | Lower required edge (cost/kATR) vs more overlap. Matches longer realised holds. **Only if** the §2.4 holding-time diagnostic shows holds ≫ 1 h, and the owner accepts both the `TARGET_ATR` change and a horizon **outside the stated M15–H1 range**. Otherwise use k = 0.75 with N = 2 (30 min), which stays within range. | **High–Med** (directly moves the cost gate) | M |
| 4 | **Time decay** c = 0.5 (linear) | Robustness to the 2020–26 shift vs losing old data | Med | M |
| 5 | **Feature pruning by nested clustered-MDA stability** (keep clusters with MDA > 0 in ≥ 70% of the inner folds) | Variance reduction and stability across eras | Med | M |
| 6 | **Hit × direction decomposition**: `P(hit)` model × `P(up \| hit)` model | Exploits the stronger predictability of vol. Can sharpen the edge in the tradable region. | Med | L/M |
| 7 | **Calibrator arm**: BCTS (temperature + class bias), or isotonic if n_eff ≥ 10k | ECE gate stability under prior shift | Med for the ECE gate, Low for expectancy | M |
| 8 | **Combination of the winners of 2–7** | Checks that one-at-a-time gains add up | Required | — |
| 9 | **Stronger regularisation preset**: `num_leaves` 7, `min_data` ×2, `extra_trees`; **or** one pre-registered nested-tuning procedure counted as a single config | Over- vs under-fitting at the chosen feature set | Low–Med | M |
| 10 | **Vol-model WAIT gate** (backlog #139): block-only, from a vol forecast, with persistence as the baseline | Removes trades where the *forecast* ATR makes cost/ATR too high, or the regime is unforecastable | Med for expectancy (system-level) | M (vol is forecastable) |
| 11 | **Cross-market**: XAGUSD return/ATR, gold/silver ratio z-score, EURUSD return/ATR | The USD/metals factor | Med if a live source exists. **Blocked on #141.** | L/M |
| 12 | **Combination 2**: #8 + any winners of #9–11 | Additivity | Required if anything won | — |
| 13 | **CUSUM-event sampling, with M05 gated on the same events** | Overlap reduction and informative sampling. A system change. | Low–Med | L |
| 14 | **Meta-labeling arm** (#140) | P(win) for strategy proposals. Needs the strategy replay seam. | Low per config: expensive, small sample | L |
| 15 | **CatBoost swap** of the current best | A different regulariser (ordered boosting) | Low | L |
| 16 | **Two-config ensemble** (the top two by out-of-sample, averaged probabilities, recalibrated) | Variance reduction | Low–Med for Brier, Low for expectancy | L/M |
| 17–20 | **Reserve (4)** | Bug-fix reruns (pre-register whether these count), or a tie-break combination | — | — |

**Stopping rules:**
- If **#1 and #2 both show Brier skill CIs including 0** and system expectancy ≤ 0 at stressed cost, **stop at ≤ 4 configs** and report "no edge". Do not burn budget on #9–16. The stop rule exists for this, and a low N preserves the DSR for a future protocol version.
- If #3 is not admissible, because `TARGET_ATR` is locked or the holding-time diagnostic says H1 is fine, its slot moves to the reserve.

**Spend profile.** 8 configs for structure (1–8). 4 for system-level and data levers (9–12). 4 speculative (13–16). 4 reserve. Expect to stop well before 20 if nothing beats the noise floor by #8.

---

## Sources

**Primary and high quality:**
- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley. Relevant chapters:
  - ch. 2: bars, CUSUM;
  - ch. 3: triple-barrier, meta-labeling;
  - ch. 4: uniqueness, sequential bootstrap, time decay;
  - ch. 7: purged CV, embargo;
  - ch. 8: MDI, MDA, SFI;
  - ch. 11–12: backtest overfitting, CPCV;
  - ch. 14: PSR/DSR.
  
  https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086 (nf)
- López de Prado, M. (2020). *Machine Learning for Asset Managers*. Cambridge Elements. Ch. 6: clustered MDA/MDI. Ch. 8: false strategy theorem, testing-set overfitting. https://doi.org/10.1017/9781108883658 (nf)
- Bailey, Borwein, López de Prado & Zhu (2017). The Probability of Backtest Overfitting. *J. Computational Finance* 20(4).
  - https://papers.ssrn.com/abstract=2326253 (403 this session)
  - https://www.risk.net/journal-of-computational-finance/2471206/the-probability-of-backtest-overfitting
  - Open copy: https://escholarship.org/uc/item/4w1110bb
- Bailey & López de Prado (2014). The Deflated Sharpe Ratio. *J. Portfolio Management* 40(5). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 (403 this session)
- Bailey & López de Prado (2012). The Sharpe Ratio Efficient Frontier (PSR, MinTRL). *J. Risk* 15(2). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643 (nf)
- López de Prado & Lewis (2019). Detection of false investment strategies using unsupervised learning methods. *Quantitative Finance* 19(9), 1555–1565. https://ideas.repec.org/a/taf/quantf/v19y2019i9p1555-1565.html
- Niculescu-Mizil & Caruana (2005). Predicting Good Probabilities with Supervised Learning. ICML. https://www.cs.cornell.edu/~alexn/papers/calibration.icml05.crc.rev3.pdf (fetched, but the PDF text was not extractable; the summary is from memory)
- Platt (1999). Probabilistic outputs for support vector machines. (nf)
- Zadrozny & Elkan (2002). Transforming classifier scores into accurate multiclass probability estimates. KDD. (nf)
- Guo, Pleiss, Sun & Weinberger (2017). On Calibration of Modern Neural Networks. ICML. https://arxiv.org/abs/1706.04599 (abstract verified)
- Kull et al. (2019). Beyond temperature scaling: Dirichlet calibration. NeurIPS. https://arxiv.org/abs/1910.12656 (nf)
- Alexandari, Kundaje & Shrikumar (2020). Maximum likelihood with bias-corrected calibration is hard-to-beat at label shift adaptation. ICML. https://arxiv.org/abs/1901.06852 (nf)
- Roelofs, Cain, Shlens & Mozer (2022). Mitigating Bias in Calibration Error Estimation. AISTATS.
  - https://arxiv.org/abs/2012.08668
  - https://proceedings.mlr.press/v151/roelofs22a.html
- Gibbs & Candès (2021). Adaptive Conformal Inference Under Distribution Shift. NeurIPS. https://arxiv.org/abs/2106.00170 (nf)
- LightGBM docs, Parameters Tuning: https://lightgbm.readthedocs.io/en/latest/Parameters-Tuning.html (fetched). Parameters (`multiclass` vs `multiclassova`, `monotone_constraints`): https://lightgbm.readthedocs.io/en/latest/Parameters.html (nf)
- Grinsztajn, Oyallon & Varoquaux (2022). Why do tree-based models still outperform deep learning on tabular data? NeurIPS D&B. https://arxiv.org/abs/2207.08815 (nf)
- McElfresh et al. (2023). When Do Neural Nets Outperform Boosted Trees on Tabular Data? NeurIPS D&B. https://arxiv.org/abs/2305.02997 (abstract only; no per-library numbers)
- Corsi (2009). A Simple Approximate Long-Memory Model of Realized Volatility. *J. Financial Econometrics* 7(2). (nf)

**Market-specific (gold or intraday):**
- Xu, Bouri, Saeed & Wen (2020). Intraday return predictability: Evidence from commodity ETFs and their related volatility indices. *Resources Policy* 69. https://ideas.repec.org/a/eee/jrpoli/v69y2020ics030142072030862x.html (abstract-level)
- Gao, Han, Li & Zhou (2018). Market intraday momentum. *J. Financial Economics* 129(2), 394–414. https://econpapers.repec.org/RePEc:eee:jfinec:v:129:y:2018:i:2:p:394-414
- Zhou, I. Intraday momentum in Chinese metal futures (gold): night-session effects. https://acfr.aut.ac.nz/__data/assets/pdf_file/0008/686816/4a-Z-Ivy-Zhou.pdf (working paper; low)
- Cai, Cheung & Wong (2001). What moves the gold market? *J. Futures Markets* 21(3), 257–278. https://ideas.repec.org/a/wly/jfutmk/v21y2001i3p257-278.html
- Smales (2015). Macro announcements and gold futures (VAR-GARCH). *Applied Economics Letters*. https://informahealthcare.com/doi/abs/10.1080/13504851.2014.972538 (abstract-level)
- Hauptfleisch, Putniņš & Lucey (2016). Who sets the price of gold? London or New York. *J. Futures Markets*. https://opus.lib.uts.edu.au/bitstream/10453/41414/4/GoldILS%20JFutMkt%20Forthcoming.pdf (nf)
- Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ (arXiv preprint; feature importance unstable across walk-forward folds). https://arxiv.org/pdf/2605.17724 (low; search snippet only)

**Kaggle (practitioner; low to medium, leakage caveats):**
- Jane Street Market Prediction (2021), 1st place: a supervised autoencoder + MLP, retrained per fold to avoid label leakage. Sample weighting credited as the main boost. The winner's exact CV scheme is unverified.
  - https://forum.numer.ai/t/autoencoder-and-multitask-mlp-on-new-dataset-from-kaggle-jane-street/4338
  - https://bullettech.github.io/BulletTech/Main_Course/Machine_Learning/2021-09-02-Kaggle-janestreet
- Optiver Realized Volatility (2021). Top solutions reportedly reconstructed the shuffled `time_id` order from price tick size, which is a leakage lesson. The mechanism is unverified at the primary source. https://chrisrichardmiles.github.io/chrisrichardmiles/projects/optiver/index_optiver.html
- **Lesson [M]:** on noisy financial targets, the competition winners' edge came from validation discipline and leak-awareness more than from model family.
