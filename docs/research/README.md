# docs/research — index & status

Research notes: library/broker/model investigations that inform PRDs and ADRs. Status: `LIVING` = keep current · `SNAPSHOT` = point-in-time (don't edit,
add new) · `ARCHIVED` = superseded · `TEMPLATE`.

> **Start here:** master plan §5.2 lists what must be verified in implementation (library versions, EA/Python bridge, DB migration tool, vector index, chart license).

| File | Status | What it is |
|------|--------|-----------|
| `2026-11-28-m04-protocol-v1.md` (+ `-targets-research.md`, `-targets-calc.py`) | SNAPSHOT | Protocol v1, pre-registered before any real-data fit and certified by the agent under owner delegation. Binding gates are assumption-light: Brier skill lower bound plus a sign test, ECE, system expectancy lower bound at stressed cost, deflated Sharpe at the number of configs tried, PBO, and regime buckets with a Holm correction. Also fixed: the regime rule, an 18-month single-use holdout, a 20-config budget, and pass/inconclusive/fail outcomes. The likely outcome is stated up front: did not meet targets. #132 |
| `2026-11-28-m04-quant-upgrade-proposals.md` | SNAPSHOT | Five external model-upgrade proposals (cross-market, meta-labeling, volatility model, weighting/horizon, Shadow data), each evaluated as an ablation arm under one protocol budget. Found that EV and the labels must price the same symmetric ATR_M15 event. Rejected any M06 model input as an owner decision. #120 |
| `2026-11-28-m04-dataset-training-plan.md` | SNAPSHOT | M04 training plan, reviewed against the master plan and the code: triple-barrier per direction (meta-labeling later), one stationary transform path for train and inference, Dukascopy backfill through the same recorder, embargo, a holdout locked before training, and no LLM re-interpretation of past news. All numbers `[PROPOSED]`; nothing trained — #48 |
| `2026-11-27-m03-llm-provider-opencode-go.md` (+ `m03-llm-eval/`) | SNAPSHOT | OpenCode Go endpoint/allowance, external benchmarks, M03 model benchmark v1 + v2 (191 graded cases, blind second labels, end-to-end through M05); recommends `qwen3.8-flash` (fallback `deepseek-v4.1-flash`) — #97, PRD #98 |
