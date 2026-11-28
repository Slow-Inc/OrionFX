---
name: prd-via-to-prd
description: "In OrionFX, break every module's PRD with /to-prd (local, model-invocable) and publish it to the module's PRD issue before implementing"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 26a3277e-796d-47a7-9572-6fc1aff61f48
  modified: 2026-11-26T21:28:17.993Z
---

Before implementing each module, run the `to-prd` skill (the copy in `~/.claude/skills/to-prd` is model-invocable) and publish its output, bilingual EN + full Thai mirror, as the body of that module's PRD issue (a sub-issue of plan tracker #4). Keep the master plan §12 spec at `docs/prd/PRD-XX-*.md` as the detailed module spec, and close the PRD issue when the implementation merges.

**Why:** the user restated the implementation goal on 2026-11-27 with an explicit "แตก PRD( /to-prd ) ก่อนเริ่มทำในแต่ละส่วน".
**How to apply:** for each module, create the PRD issue, run /to-prd, publish, then slice issues → TDD → gates → PR. Write long bodies with the Write tool, not a bash heredoc (heredocs of Thai text broke the shell). Related: [[reply-in-thai]].
