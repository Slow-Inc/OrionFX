# Domain Docs

How engineering skills should consume this repo's domain documentation when exploring.

## Layout: Single-context

One `CONTEXT.md` at the root covers the whole codebase.

```
/
├── CONTEXT.md                    ← system context; defers to UBIQUITOUS_LANGUAGE.md
├── UBIQUITOUS_LANGUAGE.md        ← canonical term glossary
├── docs/plans/master_planV1.md   ← requirements source ([LOCKED] / [PROPOSED] / [TBD])
├── docs/adr/                     ← architectural decision records
└── backend/ frontend/ mt5/       ← (planned, master plan §7.1)
```

(Revisit as multi-context — a root `CONTEXT-MAP.md` pointing at one `CONTEXT.md` per
context, plus context-scoped `docs/adr/` — once module boundaries harden into separate packages.)

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the system-context doc; the **canonical term glossary is `UBIQUITOUS_LANGUAGE.md`** (`CONTEXT.md` points at it and defers to it on any conflict).
- **`docs/plans/master_planV1.md`** — requirement IDs (TRD/PFT/RSK/…), shared business rules (§4), module ownership (§2).
- **`docs/adr/`** — read ADRs touching the area you're about to work in before proposing alternatives.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't
suggest creating them upfront. The producer skill (`/grill-with-docs` → `/domain-modeling`)
creates them lazily when terms or decisions actually get resolved.

## Use the glossary's vocabulary

When your output names a domain concept (issue title, refactor proposal, hypothesis, test
name), use the term exactly as defined in `UBIQUITOUS_LANGUAGE.md`. Don't drift to synonyms the glossary
avoids. If a concept isn't in the glossary yet, that's a signal — either you're inventing
language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly instead of silently overriding:

> _Contradicts ADR-<NNNN> (<one-line decision>) — but worth reopening because…_

The same applies to a [LOCKED] master-plan item, except the route is a Change Request (master plan §13.1), not an ADR.
