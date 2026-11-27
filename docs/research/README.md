# docs/research — index & status

Research notes: library/broker/model investigations that inform PRDs and ADRs. Status: `LIVING` = keep current · `SNAPSHOT` = point-in-time (don't edit,
add new) · `ARCHIVED` = superseded · `TEMPLATE`.

> **Start here:** master plan §5.2 lists what must be verified in implementation (library versions, EA/Python bridge, DB migration tool, vector index, chart license).

| File | Status | What it is |
|------|--------|-----------|
| `2026-11-28-m04-dataset-training-plan.md` | SNAPSHOT | M04 training plan, reviewed against the master plan and the code: triple-barrier per direction (meta-labeling later), one stationary transform path for train and inference, Dukascopy backfill through the same recorder, embargo, a holdout locked before training, and no LLM re-interpretation of past news. All numbers `[PROPOSED]`; nothing trained — #48 |
| `2026-11-27-m03-llm-provider-opencode-go.md` (+ `m03-llm-eval/`) | SNAPSHOT | OpenCode Go endpoint/allowance, external benchmarks, M03 model benchmark v1 + v2 (191 graded cases, blind second labels, end-to-end through M05); recommends `qwen3.8-flash` (fallback `deepseek-v4.1-flash`) — #97, PRD #98 |
