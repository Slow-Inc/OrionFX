# M04: five model-upgrade proposals, evaluated against OrionFX

**Status:** SNAPSHOT (2026-11-28). Don't edit it; add a newer note when you revisit.
**Source:** an external review (Gemini) proposed five "high-ROI" upgrades over the V1 chart-only LightGBM plan. The owner asked for the proposals to be checked with the advisor and the plan changed. This note records the proposals, our evaluation, and what the plan now says. The LIVING plan is `docs/agents/training-loop.md` → "Research backlog".
**Unverified claims.** These are not facts until measured on our data:
- Gemini's figures: "70–80% of the time is noise", "volatility is many times more accurate than direction", "most MT5 brokers give XAGUSD/USDX/US500 free".
- Our own claim that Dukascopy has intraday XAGUSD and EURUSD history.

## The rule that governs all five

Every proposal is an **arm**, ablated against the V1 baseline on the **same folds and seeds**, under one pre-registered protocol version (PRD #120 slice 5):
- **One budget.** All arms draw on the protocol's single iteration budget (20). Horizon, time decay, feature set and label type each count as configs.
- **One winner.** The holdout is single-use, so only **one** winner per protocol version reaches it.
- **Baseline first.** No arm runs before the V1 baseline exists (#127 tracer → #130 trainer → #132 protocol).

## Evaluation

| # | Proposal | Verdict | Constraints we add |
|---|---|---|---|
| 1 | **Cross-market data and session features.** DXY, yields, XAGUSD, VIX/SPX; session/liquidity structure; a pre-event countdown. | **Accept, in tiers.** | **Session and liquidity features** (distance to the previous session's high and low, the London/NY volatility ratio, spread anomalies) come from the gold bars themselves, so they go into #129 now. CAT-S02-10 Session Trading is a related row. **Cross-market symbols** (XAGUSD and a USD proxy) are a separate issue. `CandleEngine`, `CandleStore` and `build_feature_snapshot` handle one symbol, and #121 locks trading to XAUUSD. So this needs a feature-schema bump and an explicit rule: data-only symbols that can never be traded. Each symbol needs its own session schedule and point-in-time alignment. A cross-market feature enters a candidate **only if its live source exists**: a missing feed makes `predict` refuse and M05 wait. Training on Dukascopy and serving from the broker is a skew to measure. CAT-T14-01 Correlation (blocked) is related. **Yields, VIX and the event countdown** wait for point-in-time sources: free yields and VIX history are daily only, and the calendar needs licensed history with real release times (#41 / OD-14). |
| 2 | **Meta-labeling.** M05 strategies pick the setups; M04 predicts P(win). | **Accept as a research arm after the baseline.** | It changes the predictor-port contract. It has fewer samples: only bars with a proposal. Replaying strategies over history to generate proposals hits **the same seam as #127's evaluation load**: the gate reads strategy eligibility point-in-time (`decision/gate.py:56`), so today's registry yields no proposals for a 2018 `now`. It needs an explicit evaluation path for strategies too. The dataset must pin the strategy rule hash, because thresholds are [PROPOSED]. Related: CAT-A03-04 Meta-Learning (planned). |
| 3 | **Volatility / regime model.** Use it to WAIT in choppy markets and to adapt trailing and TP/SL. | **Accept the M05 WAIT half. Reject the M06 half.** | **WAIT half:** block-only, following the `NEWS_VETO` pattern: a [PROPOSED] threshold, a policy-version bump and a counterfactual report. It needs a **new evidence contract**, because the predictor port is direction-shaped. Its baseline is **persistence plus the existing M02 regime rule**, not class priors. Related: CAT-T13-01, CAT-T13-02, CAT-A01-04, CAT-S02-19, CAT-S02-20. **M06 half:** any model input into M06 trailing, TP or SL contradicts "risk is independent of AI". It is an **owner decision**, not a backlog item. |
| 4 | **Uniqueness weighting, time decay, and an M15–H1 horizon.** | **Accept.** | Uniqueness weights were already in the plan. **Pre-register** whether claim metrics are weighted. Time decay is a hyperparameter inside the budget, not a fixed constant. Longer horizons raise label overlap, and with it the uncertainty that the block bootstrap shows. |
| 5 | **Live news and real broker execution from Shadow, for V2.** | **Accept.** It was already the plan. | Blocked on #17 and #41. |
| — | No deep learning or transformers yet. | **Agree.** It was already deferred. | — |

## A finding the proposals surfaced: EV and labels must price the same event

`expected_value` (`backend/app/decision/fusion.py:269-290`) computes `TARGET_ATR · ATR_M15 · size · (2p − 1) − cost`, with `TARGET_ATR = 1`. That is a **symmetric** bet: win +1·ATR_M15, lose −1·ATR_M15, with p the probability of being right. The model's probability must price exactly that event. So:
- **Barriers.** Triple-barrier labels (#129) must use symmetric barriers at `TARGET_ATR · ATR_M15`, or EV must change to the label's real payoff. EV assumes a symmetric win and loss, so an asymmetric k1 ≠ k2 or a different ATR mis-prices every trade.
- **Time barrier.** The FLAT outcome is not in the EV formula. Either EV conditions on a barrier being hit, or it adds a third term for P(FLAT) with that outcome's payoff and its costs.
- **H1 horizon.** Moving the horizon to H1 while keeping ATR_M15 barriers changes what p means.
- **Meta-labels.** A meta-label P(win) must be defined against the same barriers.

This is its own backlog item, and it changes #129's acceptance criteria.

## What changed in the plan

- **`docs/agents/training-loop.md`:** a new "Research backlog" section, with the governing rule and the items above in priority order.
- **#129:** gains session and liquidity features and the EV/label alignment.
- **New issues:**
  - cross-market data-only symbols;
  - the meta-labeling research arm, including the strategy evaluation path;
  - the volatility-model WAIT gate.
  Each depends on #127, #130 and #132.
- **PRD #120** keeps these out of scope; a comment there points to the backlog.
