---
name: target-certification-delegated
description: Owner delegated certification of M04 training targets/protocol to the agent (with research + advisor); start the training loop without asking
metadata:
  node_type: memory
  type: feedback
  originSessionId: 26a3277e-796d-47a7-9572-6fc1aff61f48
  modified: 2026-11-28T00:24:36.855Z
---

> Owner: GitHub user `xenodeve` only. Not a grant for any other developer or agent.

Granted by the owner on 2026-11-28: "ไม่ต้องถามผม ให้ทำการพิจารณาอย่างเข้มข้นแล้วเริ่ม loop training ได้เลย".

The agent decides the PRD #120 protocol values itself:
- targets
- holdout window
- iteration budget
- regime rule
- readiness thresholds

It then starts the training loop without asking.

**Why:** the owner wants the loop running autonomously. They trust a rigorous research and advisor process more than their own certification.

**How to apply:**
- Ground every value in the research note and in advisor review.
- Record the delegation and each certified value in the protocol record and the docs, as "certified by agent under owner delegation, 2026-11-28".
- Keep the anti-overfitting rules and the stop rule. Never lower a target after seeing results; a change needs a new protocol version with its reason.

**Still outside this grant:**
- real money and Live
- MT5 and demo credentials (#17)
- licensed data whose terms forbid our use

Related: [[training-research-autonomy]].
