---
name: ui-audit
description: OrionFX audit of M01 Web Dashboard changes. Run only when files under frontend/ change. Covers accessibility, performance, responsive layout, edge states and the DESIGN.md rules. Findings only.
---

# ui-audit (OrionFX)

## Trigger

Run this skill only when the diff changes files under `frontend/`. Otherwise skip it with the reason "no frontend changes".

Read `DESIGN.md` (tokens, binding constraints, do and don't) and `PRODUCT.md` before you judge the change.

## Check

- **Accessibility:** semantic elements, labels on inputs and icon-only buttons, keyboard reach and visible focus, colour contrast, and status that is not shown by colour alone (for example a risk lock).
- **Performance:** re-renders on every WebSocket tick, unbounded lists, large objects held in state, and effects with missing cleanup.
- **Responsive layout:** narrow widths, overflow, and tables or long numbers that break the layout.
- **Edge states:** loading, empty, error, stale or disconnected data, and the risk-locked and safe-mode states. The UI must never imply a trade or fill that did not happen.
- **DESIGN.md rules:** tokens are used instead of raw values, and each binding constraint and do/don't is respected. Cite the rule you apply.

## Output section

```
### ui-audit: a11y / perf / responsive / states / DESIGN.md
- [severity] path:line — issue — rule or reason — fix
```

If there are no findings, write `- no findings`.
