# M04/M05 evaluation targets: realistic values for an XAUUSD intraday LightGBM (research notes)

Date: 2026-11-28. Scope: research only, no repo edits. Status of every number below: **[PROPOSED]**. Nothing here overrides a [LOCKED] item.

**Repo check.** I grepped `docs/plans/master_planV1.1.md`. It locks **no numeric qualification thresholds**:
- OD-08 (live qualification duration, sample and regimes; protected promotion and rollback thresholds) is still an open decision.
- §Validation (line ~783) says thresholds "must be set by X02/M04/M06/M07 before final evaluation, protected … and cannot be optimized on final holdout".
- M06 drawdown limits are user max-DD plus protected epoch and lifetime limits, still **[TBD]** (§4.3, line ~261).
- So nothing below collides with a locked value. The draft target "max DD within M06 policy" has no number behind it yet.

Context taken from `docs/research/2026-11-28-m04-dataset-training-plan.md` §4.4:
- Walk-forward: 2016-01-01 → 2025-06-30, expanding window, 6-month folds.
- Final holdout: 2025-07-01 → 2026-06-30.

Calculations are in `calc.py` in the same scratchpad folder.

Evidence labels:
- **H** = primary source text read (only Harvey–Liu and Wiecki et al.), or pure arithmetic.
- Note on DSR, PSR/MinTRL, PBO and WGC: I saw only their abstracts or listings, so read their "H" tags as **M**.
- Note on event-row URLs: several were taken from search result lists, and I did not open each page to confirm which page carries which figure.
- **M** = primary abstract or reputable secondary source; the method is sound but the inputs are assumed.
- **L** = practitioner lore, a single weak source, or my own derivation with assumed inputs.

---

## 0. Two corrections to the decision rule (read first)

### 0.1 With three classes, `(2p−1)` is not the edge
The labels are UP, DOWN and FLAT. Assume the FLAT (time-barrier) exit has gross P&L of about 0. Then, for a long position, in ATR units:

`EV/ATR = p_up − p_down − c'`, where `c' = round-trip cost / ATR(M15)`.

`(2p−1)` equals `p_up − p_down` only when `p_flat = 0`. When the model puts mass on FLAT, `TARGET_ATR·ATR·size·(2p−1) − cost` overstates EV. The safe rule is to trade only when `p_up − p_down > c' + margin`, and to estimate `E[payoff | FLAT exit]` empirically instead of assuming 0. Evidence: **H** (arithmetic).

### 0.2 The label barriers are not the execution exits
The master plan specifies:
- no normal per-position SL, only an **Emergency SL**;
- a USD profit-cycle target;
- basket trailing (master plan line ~207, RSK/PFT rows).

The ±1×ATR triple barrier is therefore a **model target**, not the P&L of live trades. The system-tier backtest must replay the real exit logic (cycle target, trailing, Emergency SL, time exit). Otherwise PF and expectancy measured on label barriers will not transfer. Evidence: **H** (from the repo's own spec).

### 0.3 Express cost in ATR units, not dollars
Gold went from about $1,100 (2016) to above $4,000–5,500 (late 2025 to early 2026; see §5). Dollar ATR(M15) and dollar spread both scale with price. A fixed "$3–6 ATR, $0.25 spread" assumption is right for only a slice of the sample.

**Action:** compute `c'` per bar from the historical ATR and the historical or modelled spread for that era. Report break-even per year. The per-year ATR(M15) values are **to be verified from data**; I have not measured them.

---

## 1. Accuracy: what is realistic and where break-even sits

### 1.1 Literature (credible versus overfit)

**Credible anchors**
- Fischer & Krauss (2018, EJOR): LSTM out-of-sample direction prediction on S&P 500 constituents, daily, 1992–2015. The abstract says outperformance was clear in 1992–2009; "as of 2010, excess returns seem to have been arbitraged away", with LSTM profitability "hovering around zero after transaction costs". https://ideas.repec.org/a/eee/ejores/v270y2018i2p654-669.html — **M** (abstract). I could **not verify** the often-quoted "~54% accuracy" figure; the FAU PDF sits behind a bot wall.
- Krauss, Do & Huck (2017, EJOR): a DNN + GBT + RF ensemble on S&P 500 earns >0.45%/day before costs. Profitability fell and "even became negative at times" in later years. https://ideas.repec.org/a/eee/ejores/v259y2017i2p689-702.html — **M**.
- Gaona, Rangel-Madariaga & Uribe (2026, Frontiers Appl. Math. Stat.): DT, RF and XGBoost at 1–30-minute horizons on 40 assets, including 10 FX pairs, strictly out of sample.
  - Precision on a "move exceeds cost" label: FX majors about 43–46%, stocks averaging 45%.
  - After costs, only 5 of 40 assets had positive average return per trade.
  - This label is **not comparable** to binary direction accuracy, but the conclusion holds: weak directional skill rarely survives costs at intraday horizons.
  - https://www.frontiersin.org/journals/applied-mathematics-and-statistics/articles/10.3389/fams.2026.1888767/full — **M** (fetched).
- Gerlein et al. (2016, Expert Syst. Appl.): ML classifiers on FX (USDJPY, EURGBP, EURUSD). The authors argue that accuracy is a poor yardstick and that profitability matters. https://researchportal.northumbria.ac.uk/en/publications/evaluating-machine-learning-classification-for-financial-trading-/ — **M**. The "accuracy near 50%" figure comes from a secondary summary and is **unverified**.
- Hsu, Taylor & Wang (2016, J. Int. Econ.): 21,000+ FX technical rules over 45 years, with a stepwise data-snooping test. The abstract reports **substantial profitability** survives the test. This is daily data, mostly emerging-market currencies, and cuts against a "nothing works" reading. https://ideas.repec.org/a/eee/inecon/v102y2016icp188-208.html — **M**. Kuang, Schröder & Wang find technical profitability in emerging FX "illusory" after data-snooping control: https://orca.cardiff.ac.uk/66242/1/WangQ%20-%20IllusoryProfitability.pdf — **M**.
- Sirignano & Cont (2019, Quant. Finance): high mid-price-direction accuracy at **tick** horizons from order-book data. This is not tradable after spread and not comparable to M15–H1 bars. https://arxiv.org/abs/1803.06917 — **M**.

**Typical overfit claims (treat as red flags)**
- An MQL5 blog reports "83.67% directional accuracy" on XAUUSD 2025, measured only above an 88% probability threshold: https://www.mql5.com/en/blogs/post/767489
- Hugging Face model cards report 77% validation accuracy for a 15-minute XAUUSD model: https://huggingface.co/JonusNattapong/xauusd-trading-ai-smc-v2
- Common causes: leakage from overlapping labels or features, no purge or embargo, random K-fold, no costs, accuracy measured on a hand-picked subset, and single-period tests.
- López de Prado lists these failure modes ("10 reasons", Pitfall #4 on labelling, plus CV leakage and backtest overfitting). https://www.garp.org/white-paper/the-10-reasons-most-machine-learning-funds-fail — **M**.
- Evidence that these claims are overfit: **M**. They are self-reported, without costs, and without walk-forward.

**Synthesis (L/M).**
- No rigorous peer-reviewed study reports out-of-sample, after-cost directional accuracy for **gold at M15–H1**; the gold-specific evidence base is thin (**L**).
- Across adjacent evidence (daily equities, intraday FX, multi-asset 1–30 minutes), credible unconditional out-of-sample direction accuracy is about **50–55%**, edges decay over time, and most do not survive costs.
- A credible target for **unconditional** non-FLAT accuracy is about **52–55%**.
- A credible target for **high-confidence traded subsets** is about **55–60%**.
- Anything above 65% on a material share of trades should be treated as a leakage alarm, not a success.

### 1.2 Break-even math (H for the arithmetic; the inputs are assumptions to verify)

The formula is `a_be = 0.5 + c / (2·(1−f)·ATR)`, where:
- `a` is accuracy on non-FLAT outcomes;
- `f` is the FLAT share among traded signals, with FLAT gross P&L assumed to be about 0;
- `size` = 1 oz and TARGET_ATR = 1.

Cost scenarios:
- (A) spread $0.25 only;
- (B) $0.25 spread + $0.07 commission ($7 per lot round trip) + $0.05 slippage = $0.37;
- (C) stressed: $0.40 + $0.07 + $0.10 = $0.57.

| Cost | FLAT share | ATR $3 | $4 | $5 | $6 | $8 |
|---|---|---|---|---|---|---|
| A $0.25 | 0 | 54.2% | 53.1% | 52.5% | 52.1% | 51.6% |
| A $0.25 | 0.3 | 56.0% | 54.5% | 53.6% | 53.0% | 52.2% |
| B $0.37 | 0 | 56.2% | 54.6% | 53.7% | 53.1% | 52.3% |
| B $0.37 | 0.3 | 58.8% | 56.6% | 55.3% | 54.4% | 53.3% |
| C $0.57 | 0 | 59.5% | 57.1% | 55.7% | 54.7% | 53.6% |
| C $0.57 | 0.3 | 63.6% | 60.2% | 58.1% | 56.8% | 55.1% |

**Conclusion.** Under realistic costs (B) with ATR of $3–6, the traded-subset hit rate must be about **54–59%** just to break even. Note also that losing exits may slip more than winning exits, which makes the barrier payoff asymmetric and raises the requirement further.

**→ The draft "≥ 53% non-FLAT accuracy" sits at or below break-even.** As an economic target it is **too loose**. It is acceptable only as a model-skill diagnostic on all bars.

### 1.3 Mapping profit factor to accuracy and per-trade Sharpe (H, arithmetic)
Payoffs are ±1 ATR, net of cost `c'`, with no FLAT:
- PF = 1.15 needs a = 56.0% at c'=0.05, 57.4% at c'=0.08, and 58.4% at c'=0.10.
- In every case the per-trade Sharpe is about **0.07**.
- PF = 1.10 corresponds to a per-trade Sharpe of about 0.048. PF = 1.30 corresponds to about 0.13.

So **PF ≥ 1.15 is a stricter requirement than 53% accuracy**, and the two draft targets are inconsistent with each other.

---

## 2. Calibration thresholds that matter for trading

**Why calibration matters more than accuracy here.** M05 trades on `p`. If `p` is over-confident by δ in the traded region, the realised edge falls by about 2δ for the binary case, or by δ_up + δ_down for three classes.
- The edge is only about 0.05–0.10 in ATR units (§1.3).
- An ECE of 0.03 concentrated in the traded bins can erase **most of the edge**.
- Empirical support from a related domain: Walsh & Joshi (2024, *ML with Applications*) find that for NBA betting, model selection by calibration beat selection by accuracy, with ROI +34.69% versus −35.17% on average. https://doaj.org/article/2e36e6f3ec6a46b7b54308dee2d11c23 — **M**. It covers one season and a different domain, so it shows direction, not magnitude.

**Estimator caveats**
- Equal-width binned ECE is biased, and the bias is **largest for well-calibrated models**. Equal-mass bins and the ECE_sweep estimator reduce the bias (Roelofs et al., AISTATS 2022). https://proceedings.mlr.press/v151/roelofs22a.html — **H/M** (abstract and secondary source).
- A global top-label ECE ≤ 0.03 is therefore both noisy and the wrong place to look.

**Precision and ECE contradict each other.** "Precision at P ≥ 0.60 of at least 58%" conflicts with ECE ≤ 0.03: a calibrated model must hit at least 60% in that bucket. Note too that the repo plan uses 0.65 as the threshold, not 0.60.
- A 58% floor tolerates 2pp or more of over-confidence exactly where the money is.
- Separately, for a three-class model with 1-ATR barriers, landing P(UP) ≥ 0.60 on 5% of bars implies an edge far above what the literature supports. The 0.60/58%/5% combination is likely **unattainable (too strict)**, while its 58% floor is **too loose on calibration**.
- Replace it with an EV-based threshold, `p_up − p_down ≥ c' + margin`, and a decision-region calibration test.

**Brier skill**
- BSS > 0 against training-fold climatology is the right baseline.
- For financial direction the true BSS is likely tiny, around 0.001–0.01 (my estimate, **L**). Per-fold sampling noise can push a real-but-small edge negative in some folds.
- "BSS > 0 in **every** fold" is therefore **too strict** in its literal form.
- With 13 OOS folds (for example, a 2016–2018 initial train, then 2019H1–2025H1), a sign test at p < 0.05 needs ≥ 10 of 13 positive. With 19 folds it needs ≥ 14. Arithmetic: **H**.

---

## 3. Statistical sufficiency

### 3.1 Multiple testing across 20 configs: DSR, MinTRL, MinBTL
Sources:
- Bailey & López de Prado (2014, JPM), *The Deflated Sharpe Ratio*. DSR is the PSR with the benchmark set to the expected maximum Sharpe ratio across N trials, adjusted for skew and kurtosis. https://papers.ssrn.com/abstract=2460551 — **H** (method).
- Bailey & López de Prado (2012, J. Risk), *The Sharpe Ratio Efficient Frontier*. Defines PSR and minimum track-record length. https://www.risk.net/journal-risk/2223785/sharpe-ratio-efficient-frontier — **H**.
- Bailey, Borwein, López de Prado & Zhu (J. Comp. Finance), PBO via CSCV. https://papers.ssrn.com/abstract=2326253 — **H**. Their companion work says walk-forward alone does not protect against overfitting.

**Expected-maximum factor** `(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))`:

| N trials | 1 | 5 | 10 | 20 | 50 |
|---|---|---|---|---|---|
| Factor | 0 | 1.19 | 1.58 | **1.90** | 2.28 |

For ±1 payoffs the excess kurtosis is negative and the skew is about 0, so the non-normality term is about 1. Assumptions:
- the cross-trial SR variance is taken as 1/T (null, independent trials), which is my simplification;
- the expected-maximum formula is as commonly quoted, not re-checked against the paper's text.

Requiring DSR ≥ 0.95 means the **observed** SR must satisfy `SR_obs·√T ≥ 1.90 + 1.645`. If the true SR equals that threshold, it passes only **50% of the time**. For **80% power**, add 0.84 to the right-hand side.

Trades needed (my derivation, **M**):

| True per-trade SR (≈ PF) | N = 1 (holdout), 50% / **80% power** | N = 20 (walk-forward), 50% / **80% power** |
|---|---|---|
| 0.048 (PF 1.10) | 1,180 / **2,680** | 5,500 / **8,360** |
| 0.07 (PF 1.15) | 550 / **1,260** | 2,570 / **3,930** |
| 0.10 (PF ≈ 1.22) | 270 / **620** | 1,260 / **1,930** |
| 0.13 (PF 1.30) | 160 / **370** | 745 / **1,140** |

**MinBTL.** For N = 20, the years of data needed so that the best of 20 **no-skill** configs is not expected to show an annualised SR of X:
- X = 1.0 → 3.6 years
- X = 1.5 → 1.6 years
- X = 0.5 → 14 years

**H** (formula), using `(factor/X)²`.

**The draft "≥ 200 trades" is too loose by about 10×.** Under a no-skill null (mean = −c'), the probability that one config shows PF ≥ 1.15 over 200 trades:

| c' | One config | Best of 20 independent configs |
|---|---|---|
| 0.05 | 4.6% | 61% |
| 0.08 | 1.8% | **30%** |
| 0.10 | 0.9% | 16% |

At 500 trades the best-of-20 chance falls to at most 7%. At 1,000 trades it is about 0. The PF **level** is fine; the **n** is not. Gate on DSR or a lower confidence bound, not on point PF.

**Calendar time.** The trade rate is an **assumption** (unknown until M05 and M06 are built): 2–4 trades/day, about 500–1,000 trades/year.
- The N = 20 walk-forward gate at PF 1.15 with 80% power (≈ 3,900 trades) needs **about 4–8 years of pooled OOS walk-forward**. The 2019H1–2025H1 OOS span (about 6.5 years) supplies this only at 3–4 trades/day. An earlier OOS start, or a smaller config budget, helps.
- The 12-month holdout gives about 500–1,000 trades. That is **below 80% power** for PF 1.15 (≈ 1,260 trades) at any assumed trade rate. Only at 4 trades/day does it reach about 50% power (≥ 550 trades). It cannot confirm PF 1.10 at all.
- **Pre-register** what happens if the holdout is underpowered: the result is "inconclusive, extend forward via Shadow", never "tune and re-test".
- Data after the holdout (2026-07-01 onward, about 5 months already exists) is a further untouched forward slice.

### 3.2 Days needed for the block-bootstrap CI lower bound to clear the majority rate
**Assumptions** (not measured):
- k = 4 trades/day;
- intra-day outcome correlation ρ of 0.1 or 0.3, giving a design effect of 1 + (k−1)ρ;
- days used as blocks;
- binomial variance 0.25.

Margin d = how far the lower bound must sit above the majority rate m. The table shows days needed at **50% power** (if true accuracy = a, the bound clears with about 50% probability). For **80% power**, multiply by about ((z + 0.84)/z)²: ×2.0 at z = 1.96, ×1.6 at z = 3.02.

| True a | m | d | 95% two-sided, ρ = 0.1 / 0.3 | Bonferroni over 20 (z = 3.02), ρ = 0.1 / 0.3 |
|---|---|---|---|---|
| 0.53 | 0.50 | 0.01 | 780 / 1,140 days (3.1 / 4.6 y) | 1,860 / 2,710 days (7.4 / 10.9 y) |
| 0.55 | 0.51 | 0.01 | 350 / 510 days (1.4 / 2.0 y) | 825 / 1,210 days (3.3 / 4.8 y) |
| 0.56–0.57 | 0.50–0.51 | 0.02 | 195 / 285 days (0.8 / 1.1 y) | 465 / 680 days (1.9 / 2.7 y) |

Two points follow:
- **A 53% target is statistically unverifiable under the 20-config correction** without about 7–11 years of OOS traded days. A 53% target therefore buys nothing.
- **Model tier versus trade tier.** Model-tier accuracy runs on **all labelled bars**, about 24k M15 bars per year. Its effective n is much larger, roughly bars ÷ (overlap length × autocorrelation), so blocks of 1 day still give about 250 blocks per year. Model-tier and trade-tier CIs must be reported separately and must not be pooled.

Block bootstrap: use a moving-block or stationary bootstrap (Politis–Romano) with block length ≥ 1 trading day, which is longer than the maximum label horizon plus the embargo. **M** (standard method; not re-sourced here).

---

## 4. Profit factor, Sharpe, and the haircut from backtest to live

**Haircut, primary source (Harvey & Liu, *Backtesting*, JPM 2015)** — https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF — **H** (PDF text read):
- "A common practice … is to discount the reported Sharpe ratios by 50%"; "The 50% haircut is only a rule of thumb."
- The haircut is nonlinear: "almost always more than and sometimes much larger than 50% when the annualized Sharpe ratio is less than 0.4", and "at most 25%" when SR > 1.0. "50% is too lenient for relatively small Sharpe ratios (< 0.4) and too harsh for large ones (> 1.0)."

**Wiecki et al. (Quantopian, 888 algorithms)** — https://c.mql5.com/forextsd/forum/208/All%20that%20Glitters%20Is%20Not%20Gold%20-%20Comparing%20Backtest%20and%20Out-of-Sample%20Performance%20on%20a%20Large%20Cohort%20o.pdf — **H** (abstract read):
- Backtest Sharpe offers "little value in predicting out of sample performance (R² < 0.025)".
- "The more backtesting … the larger the discrepancy."
- Volatility and max-DD **are** predictive.

**Suhonen, Lennkh & Perez (2017, JPM), 215 bank "alternative beta" strategies:**
- median Sharpe 1.20 in backtest versus 0.31 live, a **median 73% deterioration**;
- only 8.4% matched their backtest;
- 30% were negative live.
- https://alphaarchitect.com/beware-of-backtest-overfitting/ and https://www.risk.net/node/5297666 — **M** (secondary summaries; the primary was not read).

**Arnott, Harvey & Markowitz (2019, JFDS), backtesting protocol.** Key steps: an ex-ante hypothesis, counting every trial, and asking whether there is enough data for the complexity. https://papers.ssrn.com/abstract=3275654 — **M**.

**PF thresholds.** I found **no rigorous source** for a "deployable PF". Retail-practitioner lore suggests PF 1.1–1.5 for intraday systems and treats PF > 2 as suspicious; evidence **L**. The defensible approach is statistical:
- PF ≥ 1.15 at **stressed** cost (scenario C), with DSR ≥ 0.95 at N = 20 over the walk-forward. This implies a per-trade SR of about 0.07.
- At 2–4 trades/day that annualises to about **1.6–2.2**. After Harvey–Liu-style haircutting (about 25% for SR > 1) it is still about 1.2–1.7.
- Annualised SR above 2 for a retail intraday system should itself be treated as a red flag (**L**).

**Recommended planning haircut**
- Plan for live SR = **0.5× backtest SR** at a backtest SR of about 1–1.5. Plan for **0.25–0.35×** if the backtest SR is below 1 or the configs were heavily searched.
- Require that the haircut expectancy is still > 0 at stressed cost.

---

## 5. Market conditions to cover

### 5.1 Taxonomy
Use **ex-ante observable** variables only. Fix all thresholds from **training-fold data** before looking at OOS results.

1. **Volatility.** ATR(M15) percentile against a trailing 1-year window: Low < 33rd, Mid, High > 67th, plus Extreme > 95th. Normalising against a trailing window handles the 4–5× price-level change.
2. **Trend.** The 20-day return divided by the 20-day realised volatility, as a z-score. Up > +1, Down < −1, otherwise Range. A D1 or H4 slope/ADX variant is also fine. Pick one in advance.
3. **Session** (defined in New York time so DST is handled):
   - Asia: 18:00–02:00 NY
   - London: 02:00–08:00 NY
   - London/NY overlap: 08:00–12:00 NY (includes the 08:20 COMEX open and the 08:30 US data)
   - NY afternoon: 12:00–17:00 NY
   - Rollover: 17:00–18:00 NY
   - Evidence that sessions matter:
     - A 5-minute precious-metals study (2000–2015) finds strong intraday periodicity in returns, volatility, volume and spread tied to major market opens and closes, with the lowest spread while Europe is open. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5407636/ — **M**.
     - Price discovery is led more by New York futures than London (Hauptfleisch, Putniņš & Lucey, JFM 2016). https://opus.lib.uts.edu.au/bitstream/10453/41414/4/GoldILS%20JFutMkt%20Forthcoming.pdf — **M**.
     - At rollover, liquidity thins and quotes widen (CME). https://www.cmegroup.com/articles/2026/when-fx-futures-pause.html — **M**. XAUUSD-specific widening figures exist only in broker blogs (**L**); measure them from your own broker's ticks.
4. **News window.** ±30 minutes around tier-1 US releases: NFP, CPI, FOMC statement and press conference, PCE, GDP, retail sales, and Fed-chair testimony. Cai, Cheung & Wong (2001, JFM), using 5-minute COMEX gold: of 23 US announcements, **employment, GDP, CPI and personal income** move gold most. https://ideas.repec.org/a/wly/jfutmk/v21y2001i3p257-278.html — **M**. Until the news module exists, this bucket defaults to **no-trade**.
5. **Structural era.**
   - 2016–2021: range-bound, mostly $1,100–2,075.
   - 2022–2026: the central-bank-buying regime. Central banks bought more than 1,000 t in each of 2022, 2023 and 2024 (1,045 t in 2024), against a 473 t average in 2010–2021 (WGC). https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-full-year-2024/central-banks — **H/M**. This era includes successive record highs.
   - Tag every bar with its era. Report results per era.

### 5.2 Named stress windows

| Window | What happened | In which split | Source and strength |
|---|---|---|---|
| 2016-06-23/24 (Brexit), 2016-11-08/09 (US election) | overnight gap and shock days | WF | not verified in this pass — **L** |
| 2020-03-09 → 03-31 | COVID dash-for-cash: gold sold for liquidity, then +8.6% in the week of 23–27 March; the COMEX–London EFP broke down (NY premium up to about $100/oz versus a couple of dollars normally) | WF | https://www.financemagnates.com/analysis-retail-fx/covid-19-and-gold-what-happened-how-did-the-market-react/, https://www.lbma.org.uk/alchemist/issue-98/is-gold-still-a-safe-haven-asset — **M**. The "−12%" figure is **unverified** |
| 2020-08 | record near $2,075, then a sharp reversal; Reuters coverage of October 2025 calls that drop the steepest "since August 2020" | WF | indirect — **L/M** |
| 2022-02-24 → 03 | invasion spike | WF | **not verified in this pass** — **L** |
| 2022-03 → 2022-11 | Fed hiking cycle, persistent downtrend | WF | **no citation found in this pass** — **L**; check against price data |
| 2025-03-14 | first move through $3,000 | WF | https://www.axios.com/2025/03/14/gold-all-time-high-3000 — **M** |
| 2025-04-02 → 04-23 | tariff shock: an early-April liquidation sell-off, a record $3,500.05 on 22 April, then about −3% on 23 April | WF (last fold) | https://www.kitco.com/news/off-the-wire/2025-04-23/gold-falls-3-trump-comments-fed-and-china-boost-risk-sentiment, https://www.kitco.com/opinion/2025-04-03/profit-taking-and-liquidation-drive-gold-lower-amid-global-market-decline — **M/H** |
| 2025-10-20/21 | record about $4,381, then spot about −5.5% to −6.8%; the "largest since" ranking differs by source | **HOLDOUT** | https://www.investmentnews.com/alternatives/gold-silver-post-steepest-declines-in-years-as-rally-stalls/262658, https://www.businesstoday.in/personal-finance/investment/story/gold-and-silver-prices-drop-sharply-marking-biggest-decline-since-2013-and-2021-499097-2025-10-21 — **M** |
| 2026-01-29 → 02-02 | record about $5,595 on 29 January; spot −9.5% on 30 January (Warsh nomination); about $4,400 on 2 February | **HOLDOUT** | https://www.axios.com/2026/01/30/kevin-warsh-silver-price-today-gold, https://www.mining.com/gold-silver-prices-plunge-as-trumps-fed-chair-pick-triggers-selloff — **M** |

**Flag for the owner.** The two most extreme down-days of the decade (October 2025 and January 2026) sit **inside the locked holdout**.
- The holdout is therefore regime-atypical: the price level is high, volatility is extreme, and the ATR level is never seen in training.
- These events must **not** be used to tune.
- Pre-register that holdout results are reported both overall and per regime bucket.
- An adverse holdout result driven by those days means "the model should refuse this regime". It does not mean "retrain until it passes".

### 5.3 Coverage rule (pre-registered before any result is seen)
- **Every bucket is evaluated and reported.** Hiding a bucket is a catalog-honesty defect.
- **Model-tier bucket test.** A bucket is adequately sized at ≥ 2,000 labelled bars **and** ≥ 60 distinct trading days. Pass: the upper bound of the day-block bootstrap 95% CI for BSS is ≥ 0, meaning the bucket is not significantly worse than climatology. A non-inferiority margin is the alternative, but only once per-bucket CI widths are known.
- **Trade-tier bucket test.** A bucket is adequately sized at ≥ 300 trades. Rationale: about 275 trades give 80% power to detect a no-skill edge (−c') against a predicted +0.07 ATR (my derivation, **H/M**). Pass: one-sided 95% upper bound of net expectancy > 0, meaning not significantly losing, **and** point expectancy ≥ 0.
- **Buckets below the size floor** are reported as "insufficient". By default M05/M06 **do not trade** there (refusal-when-uncertain). They do not count toward pass or fail.
- **A failing adequately-sized bucket** has two acceptable outcomes:
  - (i) the whole model fails; or
  - (ii) a **pre-declared regime gate** (a no-trade rule in M05/M06) is added, and the **whole** walk-forward is re-run with the gate included, which counts as one more of the 20 configs.
  - Covering many conditions means evaluated, and either profitable or explicitly refused. It does not mean profitable everywhere.
- **Multiple buckets.** With about 15–25 bucket tests, apply Holm correction to the "significantly losing" tests, or say plainly that some bucket will look bad by chance.

---

## 6. Promotion: backtest → Shadow → Demo → Live

The repo already mandates this staging (VAL-01, VAL-04, VAL-05; §Validation line ~779). The plan itself notes that "demo fills may differ from live fills". Durations and sizes are **my derivations** (**L/M**); no rigorous source fixes them.

Practitioner guidance:
- Pre-commit to the paper-trading window or trade count.
- Paper fills are an upper bound on live fills.
- Changing the rules restarts the clock.
- Sources: https://www.luxalgo.com/library/concept/forward-paper-trading-confirmation/ and https://dev.to/pickuma/paper-trading-done-right-validating-a-strategy-before-real-capital-3h2j — **L**.

### Stages

| Stage | What it can prove | Minimum | Pass criterion |
|---|---|---|---|
| Walk-forward (N = 20) | that an edge exists after selection | ≥ 3,900 OOS trades at PF 1.15, or ≥ 8,400 at PF 1.10 (80% power); ≥ 3.6 years of OOS data (MinBTL) | DSR ≥ 0.95; PBO (CSCV) ≤ 0.2; PF ≥ 1.15 at stressed cost; buckets per §5.3 |
| Holdout (N = 1, touched once) | that the selected config generalises | ≥ 1,260 trades for 80% power at PF 1.15 (a 12-month holdout yields about 500–1,000, so it is likely underpowered and should be pre-registered as such) | one-sided 95% lower bound of net expectancy > 0; PF ≥ 1.10 at modelled cost; no adequately sized bucket significantly losing |
| Shadow (live data, no orders) | no live leakage; signal parity with the backtest | ≥ 300 signals **and** ≥ 3 calendar months, covering at least 3 NFP, 3 CPI and 2 FOMC events | live signal distribution matches the backtest; feature PSI < 0.25 (**L**); hit-rate CI includes the backtest haircut expectation |
| Demo (MT5 demo orders) | execution, costs, ops; **not** the edge | ≥ 300 trades **and** ≥ 3 months | realised cost ≤ 1.25× modelled cost; no rejection or ownership incidents; not significantly below the backtest (see below) |
| Live, low risk (min lot) | real fills and slippage | ≥ 200 trades **and** ≥ 3 months before any size increase | realised slippage ≤ modelled; the same falsification test |

**Why 300 trades for Shadow and Demo.** To detect, at 80% power and one-sided α = 5%, that the edge is entirely illusory (true mean −c' = −0.08 against a backtest +0.07 ATR), the requirement is ((1.645 + 0.84)/0.15)² ≈ **275 trades**. At c' = 0.05 it is about 430. Demo cannot **prove** a haircut edge: proving 0.035 > 0 needs about 5,000 trades. **Demo is a falsification test, not a confirmation.** **H/M** (derivation).

### Degradation to expect
- Plan for 50% of the backtest Sharpe (Harvey–Liu rule of thumb, **H**).
- The Suhonen median is 73% deterioration (**M**).
- Backtest Sharpe barely predicts live Sharpe (Wiecki R² < 0.025, **H**).

### Kill-switch criteria
These sit on top of M06's DD lock, which always takes priority.
1. **Drawdown.** Halt if realised DD exceeds the 95th-percentile max DD from a block-bootstrap of the backtest trade sequence over the same number of trades. Bailey & López de Prado's "triple penance" result says time under water is about 3× the time taken to reach the DD quantile. https://www.risk.net/journal-of-risk/2435335/stop-outs-under-serial-correlation-and-the-triple-penance-rule — **M**. Also halt if time under water exceeds 3× the backtest's 95th-percentile time to max DD.
2. **Performance falsification.** Once ≥ 100 trades have accrued, run a rolling one-sided test. Halt if p < 0.05 for the hypothesis "live mean ≥ 0.5 × backtest mean". A CUSUM or SPRT on the per-trade P&L is an equivalent alternative.
3. **Calibration drift.** Over a rolling 200 traded signals, halt if the upper 95% bound of the realised `hit_up − hit_down` falls below `c'`.
4. **Cost drift.** Over a rolling 50 trades, halt if the realised round-trip cost exceeds 1.5× the modelled cost.
5. **Data drift.** If any key feature's PSI exceeds 0.25, pause and review (**L**, industry convention).
6. **Resuming.** After any halt, resumption requires re-validation and owner approval (consistent with RSK-07). Never auto-resume.

---

## 7. Recommended target table ([PROPOSED])

| # | Target | Draft | Recommended value | Rationale | Evidence | Verdict on draft |
|---|---|---|---|---|---|---|
| M1 | Brier skill vs training-fold priors | > 0 in every fold | Pooled BSS block-bootstrap 95% lower bound > 0; ≥ 10 of 13 OOS folds positive (sign test, scaled to the actual fold count); no fold BSS < −0.01 | A true edge is tiny, so fold noise flips the sign; "every fold" fails real-but-small edges | M | **Too strict** (literal); add the pooled CI |
| M2 | Calibration | ECE ≤ 0.03 | Global class-wise ECE (equal-mass bins or ECE_sweep) ≤ 0.02, **and** a traded-region CI-consistency test: the mean predicted edge lies inside the 95% CI of the realised `hit_up − hit_down` (half-width about ±0.038 at 2,600 trades), **and** the realised CI lower bound is > `c'` | 0.03 is as large as the whole edge; binned ECE is biased | M | **Too loose**, and measured in the wrong place |
| M3 | Direction accuracy, non-FLAT, all bars | ≥ 53%, CI lower bound > majority | Keep as a **diagnostic**: CI lower bound > majority rate + 1pp, on day blocks | 53% is at or below break-even; it is not an economic target | H (math) / M | **Too loose** as an economic gate; fine as a diagnostic |
| M4 | Traded-subset hit rate | — (implicit in precision@0.60) | ≥ break-even for that era's `c'` + 2pp (about **56–59%** at ATR $3–6 under cost B), lower CI bound > break-even | Ties the model to the cost model; the table in §1.2 sets the level | H | **New** (replaces the 53%) |
| M5 | Precision@P ≥ 0.60 ≥ 58%, coverage ≥ 5% | as drafted | Drop it. Use an EV threshold `p_up − p_down ≥ c' + 0.02`; coverage ≥ 2% of bars **and** ≥ 500 trades/year; enforce calibration in that region via M2 | 58% at P ≥ 0.60 contradicts calibration; 0.60 three-class on 5% of bars is implausibly strong; repo plan says 0.65 | M | **Internally inconsistent** |
| M6 | No fold or regime bucket worse than baseline | as drafted | §5.3 rule: size floors (≥ 2,000 bars and ≥ 60 days for the model tier; ≥ 300 trades for the trade tier); "worse" means **significantly** worse; failing buckets go to a pre-declared no-trade gate; Holm across buckets | Without size floors and a significance test, "worse" is noise | M | **Unsupported** as written (undefined n and test) |
| S1 | Net expectancy after costs | > 0 | One-sided 95% **lower bound** > 0 at modelled cost **and** point estimate > 0 at stressed cost (C); expressed in ATR units per era | A point estimate > 0 passes noise | H | **Too loose** |
| S2 | Profit factor | ≥ 1.15 | ≥ 1.15 at **stressed** cost on walk-forward; ≥ 1.10 at modelled cost on holdout | Equals a per-trade SR of about 0.07, roughly an annual SR of 1.6–2.2; the level is reasonable | L (level) / H (mapping) | Level **OK**; must be paired with n |
| S3 | Trade count | ≥ 200 | Walk-forward: DSR ≥ 0.95 at N = 20, i.e. ≥ **3,900** trades at PF 1.15 with 80% power (2,600 is the 50%-power floor); holdout: ≥ **1,260** trades for 80% power (N = 1). Below that, pre-register the holdout as "underpowered: a non-rejection is not a pass", and extend forward via Shadow | 200 trades: a 16–61% chance one of 20 no-skill configs shows PF ≥ 1.15 | H | **Too loose (≈ 10×)** |
| S4 | OOS period | — | ≥ 3.6 years of pooled walk-forward OOS (MinBTL, N = 20, SR 1); holdout 12 months, plus the existing post-holdout data from 2026-07 onward | MinBTL | H | **New** |
| S5 | Max DD | within M06 policy | M06 numbers are **[TBD]**. Require the bootstrap 95th-percentile DD over 1 year of trades at the planned lot to be ≤ 50% of the user max-DD, leaving headroom for the live haircut and gaps | Emergency SL and gaps can exceed the modelled DD (plan §273) | L | **Unsupported** until M06 sets numbers |
| S6 | Overfitting control | — | PBO (CSCV over the 20 configs) ≤ 0.2; log every config tried, including failed ones | Bailey et al. | H (method) / L (0.2 cutoff) | **New** |
| B1 | Iteration budget | 20 configs | Keep 20, counted across **everything** that sees OOS data (features, labels, thresholds, regime gates); use N = 20 in the DSR; reset only with a fresh untouched slice | Harvey–Liu; Arnott, Harvey & Markowitz | H | **OK**, but define what counts as a config |
| P1 | Shadow / Demo / Live minimums | — | Shadow ≥ 300 signals and ≥ 3 months; Demo ≥ 300 trades and ≥ 3 months; Live min-lot ≥ 200 trades and ≥ 3 months before scaling; kill switches per §6 | Falsification power of about 275 trades; covers ≥ 3 NFP/CPI cycles | L/M | **New** (feeds OD-08) |
| P2 | Expected live degradation | — | Plan for live SR = 0.5× backtest (0.25–0.35× if backtest SR < 1); the haircut edge must stay > 0 at stressed cost | Harvey–Liu; Suhonen; Wiecki | H/M | **New** |

**Bottom line.** The drafts mix levels that are roughly right (PF 1.15, BSS > 0, 20 configs) with three kinds of problem:
- sample sizes that are far too small (200 trades);
- economic thresholds below break-even (53%);
- a calibration and precision pair that contradicts itself (ECE 0.03 against 58% at P ≥ 0.60).

The binding constraint for "usable in reality" is **cost/ATR per era**. Everything should be expressed in ATR units and gated on confidence bounds, not point estimates.
