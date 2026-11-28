# M04 protocol v1: pre-run viability result (§6)

- **Status:** SNAPSHOT (2026-11-28). **PROVISIONAL**: the code that produced it is unreviewed. The result is final only after a rerun from `main`, once that code has been reviewed and merged.
- **Verdict:** **NON-VIABLE** under the pre-declared §6 rule. No k in the grid meets both V1 and V2. **No v1 bound was changed.**
- **Parent:** #132 (PRD #120). Protocol: `2026-11-28-m04-protocol-v1.md` §6. Full derived table: `m04-v1-viability/viability-hd-xau-v2.md`. It holds derived statistics only, no raw data.

## Provenance

| Item | Value |
|---|---|
| Dataset | HistData XAUUSD M1 (owner decision, 2026-11-28), imported as `hd-xau-v2` with corrected America/New_York DST timestamps. The correction was checked against Dukascopy UTC: median \|diff\| 0.0 at +4 h in summer and +5 h in winter. |
| Code | `origin/feat/128-dukascopy-source` @ 749d9a7. **UNREVIEWED.** |
| Command | `scripts/viability.py --db hd2.db --version hd-xau-v2`, run on Colab (14.9 s) |
| Era | The most recent development era: 2022-01-01 → 2023-11-28 |
| Horizon | H1 |
| k grid | {1.0, 1.25, 1.5, 2.0}·ATR_M15 |
| Cost | `STRESSED_COST`, $1.07 per oz, in fixed dollars |
| Rule | V1: FLAT share ≤ 60%. V2: median stressed cost/d ≤ 0.10. Pick: the smallest k that meets both. |
| What was read | Price and label statistics only. No model was fitted, and neither the holdout nor the unread slice was touched. |

## Result (all rows in the era)

| k | Rows | No label | Dual touch | UP | DOWN | FLAT | cost/d median | p75 | p90 | V1 | V2 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0 | 41,647 | 25.2% | 0.2% | 34.8% | 35.4% | 29.8% | 0.524 | 0.682 | 0.844 | pass | **fail** |
| 1.25 | 41,647 | 25.2% | 0.2% | 27.7% | 27.9% | 44.4% | 0.419 | 0.545 | 0.675 | pass | **fail** |
| 1.5 | 41,647 | 25.2% | 0.1% | 21.7% | 21.4% | 56.9% | 0.350 | 0.454 | 0.563 | pass | **fail** |
| 2.0 | 41,647 | 25.2% | 0.1% | 12.8% | 12.8% | 74.4% | 0.262 | 0.341 | 0.422 | **fail** | **fail** |

The bound is 0.10. The closest any k comes is a median cost/d of 0.26, at k = 2.0, where V1 also fails.

**Median cost/d by session and year:**

| k | Asia | London | NY | 2022 | 2023 |
|---|---|---|---|---|---|
| 1.0 | 0.659 | 0.553 | 0.409 | 0.499 | 0.595 |
| 1.25 | 0.527 | 0.443 | 0.327 | 0.399 | 0.476 |
| 1.5 | 0.439 | 0.369 | 0.272 | 0.332 | 0.397 |
| 2.0 | 0.329 | 0.277 | **0.204** | 0.249 | 0.297 |

NY has the lowest cost/d, but even NY at k = 2.0 is twice the bound, with a FLAT share of 77%.

**Dual-touch rate.** It is about 0.1–0.4% in every group, under the 1% switch. The exclusion bias is small.

## Arithmetic: why no k in the grid can pass

- V2 needs 1.07 / (k·ATR_M15) ≤ 0.10, so **k·ATR_M15 ≥ $10.70**.
- From the k = 1.0 row, the median ATR_M15 in the era is about 1.07 / 0.524 ≈ **$2.0**.
- So V2 needs roughly **k ≈ 10.7 / 2.0 ≈ 5**: barriers about 5 ATR_M15 from entry.
- At an H1 horizon (4 M15 bars), the label would then be **essentially all FLAT**. The FLAT share is already 74% at k = 2.0, and the driftless model gives P(no touch) → 1 as k grows.
- The grid stops at 2.0 by design. A wider k cannot satisfy V1 at H1, so the rule ends "non-viable", as pre-declared.

So the barrier is not the binding constraint. The binding constraint is **cost against the size of an H1 move** in this era. A $1.07 stressed round trip is about half of one median ATR_M15.

## Data-quality caveats (recorded honestly)

1. **The import still reports `fits=False`.** It found:
   - in_closed: 6,268 M5 bars, probably holidays and early closes, which the schedule has no calendar for;
   - 30,703 unexpected M5 gaps;
   - 1,431 OUT_OF_ORDER and 1,025 DUPLICATE bars.
2. **The no-label rate in 2023 is 46.6%, against 8.4% in 2022.** **Root cause: gaps in the HistData source.**
   - A normal month holds about 5,800–6,300 M5 bars. These are the M5 bar counts per month in `hd-xau-v2`:

     | Month | M5 bars |
     |---|---|
     | 2023-01 | 5,724 |
     | 2023-02 | 4,950 |
     | 2023-03 | 4,583 |
     | 2023-04 | 3,816 |
     | 2023-05 | 4,518 |
     | 2023-06 | 4,332 |
     | 2023-07 | 4,224 |
     | 2023-08 | 6,348 |
     | 2022, every month | 5,490–6,348 |

   - So HistData is missing roughly **20–35% of bars from February to July 2023**.
   - Under the rule "a missing bar gives no label" (§4), every row whose H1 window touches a missing bar becomes no-label.
   - **Consequences:**
     - **2023 is under-sampled in this era.** Its statistics rest on fewer rows, and possibly on a non-random subset of hours.
     - **Before any rerun**, February–July 2023 must be filled from a complete source: Dukascopy (slow, but complete), or another source.
   - **Effect on the verdict: none.** 2022 alone has an 8.4% no-label rate, and it fails V2 at every k as well.
3. **The code is unreviewed** (749d9a7, on a feature branch).

**Do the caveats change the verdict?**
- Missing labels shrink the sample. They do not obviously bias cost/d, since cost/d uses the ATR on labelled rows.
- 2022 alone, with only 8.4% no-label, also fails V2 at every k (median 0.25–0.50).
- So the caveats are unlikely to flip the verdict. Still, the result stays **provisional** until a clean rerun from reviewed code on `main`.

## What this does not say

- It does **not** say gold has no predictable structure. No model was fitted.
- It **does** say that under protocol v1's geometry (an H1 horizon, k ≤ 2) and its fixed stressed cost, the cost hurdle sits far above the credible edge range (§6 derivation: 0.10). No config is worth spending.
- v1 spends **0 configs**. The terminal state is "non-viable". A v2 needs its own reason, and must be judged on data no v1 run has used for claims. The v2 options are listed, not certified, in protocol v1 §12.

## Who verified what

- **The coordinator** ran the measurement on Colab, from the unreviewed branch.
- **This note** checks the verdict against the pre-declared rule, and the arithmetic (ATR about 1.07/0.524; k ≈ 5) by hand.
- **The independent recomputation** in protocol rule 6 has **not** been done.
