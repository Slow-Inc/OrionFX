# Architecture Decision Records

Each ADR captures one significant, hard-to-reverse decision: its context, what was chosen,
the alternatives rejected, and the consequences. They document decisions **already in the
codebase** (unless marked *pending*), so a new maintainer — human or agent — can recover the
*why* without re-deriving it. A decision that overturns an earlier one marks the old ADR **Superseded**.

| # | Title | Area | Status |
|---|-------|------|--------|

_No ADRs yet. Master plan §10 P0 expects ADRs alongside the module PRDs (e.g. EA/Python bridge protocol, DB migration tool, vector index — all [PROPOSED]/[TBD] in master plan §5.2)._

## Conventions

- Filename: `NNNN-kebab-title.md`, zero-padded. **Numbers must be unique** across *all* branches.
- Body: title line, a status/context bullet block, then `## Context`, `## Decision`,
  `## Alternatives considered`, `## Consequences`.
- Ground every claim in the code as it is **now**; cite `file:line`.
- An ADR never overrides a [LOCKED] master-plan requirement; that needs a Change Request (master plan §13.1).
