---
name: master-plan-authority
description: docs/plans/master_planV1.1.md is the requirements source; [LOCKED] items change only via Change Request
type: project
---

`docs/plans/master_planV1.1.md` (§0.1) ranks authority: latest [LOCKED] requirement > reviewed PRD > technical design > implementation. [PROPOSED] items may change with evidence and review; [TBD] items must not be assumed approved, and a safety-critical TBD blocks Live.

**Why:** the plan is shared by every module owner and agent; a silent local change breaks cross-module contracts.
**How to apply:** when work conflicts with a [LOCKED] item, write a Change Request (§13.1) instead of editing behaviour. Related: [[no-real-money]].
