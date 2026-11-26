---
name: no-real-money
description: No live funds, credentials, or real account data anywhere during build and test
type: project
---

During build and test, use only a fake broker, historical replay, or an MT5 Demo account. No API key, password, or real account data goes into the repo, test fixtures, logs, or prompts (master plan §0.2). The LLM never sends trade orders directly; only M06 sends orders, through the EA.

**Why:** this is a trading bot that can lose real money; the master plan lists these as prohibitions.
**How to apply:** any change that crosses a secret or broker boundary gets `/security-review`; a secret blocked by push protection gets rotated, never bypassed. Related: [[master-plan-authority]].
