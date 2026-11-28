---
name: training-research-autonomy
description: "Owner lets the agent research and improve M04 models and training on its own, within PRD #120 protocol and ADR 0001 limits"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 26a3277e-796d-47a7-9572-6fc1aff61f48
  modified: 2026-11-27T20:55:23.601Z
---

The owner granted this on 2026-11-28. The agent may research independently in the OrionFX training loop and improve models and the training process from collected data, without asking first.

**Why:** the owner's goal is a 100% autonomous system. They chose policy-level approval (ADR 0001) over approving each step.

**How to apply:**
- Explore freely on development data: walk-forward folds and the debug window.
- Make claims only under a pre-registered protocol version.
- Never tune on the holdout or the unread slice.
- Targets and thresholds change only when the owner certifies a new policy version. Propose changes; never adopt them.
- Autonomous promotion covers Shadow and Demo only. Real-money Live stays with the owner.
- Generated code goes through PR, T4 gates and tests, and runs with no credentials.
- Write findings to `docs/research/`.

The full text is in OrionFX `CLAUDE.md` → "Training-loop research autonomy". Related: [[prd-via-to-prd]].
