# PRD-X01 — Architecture & Integration (shared contracts + P1 foundation)

- Module ID: X01
- Owner: agent (Claude), under developer directive 2026-11-27
- Reviewers: M06, M08 owners (contracts they consume); X02 (acceptance)
- Status: Implemented (see issue #5)
- Version: 0.1
- Parent: `docs/plans/master_planV1.md` (V1.0)
- Target phase: P0–P1
- Requirement IDs: INT-01, INT-02, INT-03, INT-04; SYS-01; cross-cutting §8.1–8.2, §9.1, §9.3

## 1. Summary & product goal

Every module talks through one set of typed contracts so that no two modules disagree about units, time, IDs, event shapes, or who may change which state. Success = a module can be built and tested against `backend/app/core/` alone, and a contract change shows up as a failing test and a schema diff, not a runtime surprise.

## 2. Scope

**In scope (MVP):**
- Repository skeleton for the Python backend: `pyproject.toml`, locked dependencies (`uv.lock`), ruff, mypy (strict), pytest; package `backend/app`.
- `backend/app/core/`: value conventions (money, lots, prices, probabilities, UTC time, IDs), operating mode, the common event envelope (§8.1), payload schemas for every event in the §8.2 catalog, the control-lifecycle and cycle-lifecycle state machines with legal transitions and the safety priority order (§4.5, §9.1).
- Generated JSON Schemas in `docs/contracts/` for non-Python consumers (the dashboard, the MQL5 bridge later).
- CI `lint` / `typecheck` / `test` jobs; local `verify` in `.claude/t4.json`.

**Advanced (not here):** transport between processes (M08), schema version negotiation beyond `schema_version` string equality.

**Non-goals:** any trading behaviour; any module's internal logic.

**Assumptions:** single Python process topology for MVP (M08 decides); `schema_version` starts at `"1.0"`.

## 3. Actors & use cases

- A producer module builds a payload, wraps it in an `EventEnvelope`, hands it to the journal (M08). Validation fails loudly on wrong units, naive/non-UTC time, float money, unknown event type, or a payload that does not match the event type.
- A consumer deduplicates by `event_id` (at-least-once delivery, §8.2 rules).
- M06 asks the state machine whether a control transition is legal before persisting it; an illegal one raises.

## 4. Functional requirements

| Local ID | Master ID | Requirement | Priority | Phase | Acceptance test |
|---|---|---|---|---|---|
| X01-FR-001 | INT-01 | SHALL define one envelope with the §8.1 fields; `occurred_at`/`recorded_at` UTC-aware | Must | MVP | `test_envelope_*` |
| X01-FR-002 | INT-01 | SHALL define a payload schema for every §8.2 event type; envelope rejects a payload of the wrong type | Must | MVP | `test_catalog_*` |
| X01-FR-003 | INT-01 | Money, lots, prices, thresholds SHALL be `Decimal`; float input rejected | Must | MVP | `test_money_rejects_float` |
| X01-FR-004 | INT-01 | Unknown values SHALL be `None`, never zero-filled | Must | MVP | schema review + nullable fields |
| X01-FR-005 | INT-03 | SHALL encode legal control transitions (§9.1) and cycle transitions; illegal → `IllegalTransition` | Must | MVP | `test_states_*` |
| X01-FR-006 | INT-03 | SHALL encode the safety priority: emergency/DD > close-in-progress > profit exit/trailing > restricting control > entry/recovery (§4.5) | Must | MVP | `test_priority_order` |
| X01-FR-007 | INT-03 | Leaving `SAFE_MODE` SHALL NOT clear a risk lock (§9.1) | Must | MVP | `test_safe_mode_exit_keeps_lock` |
| X01-FR-008 | INT-01 | Checked-in JSON Schemas SHALL equal the schemas generated from code | Must | MVP | `test_schemas_up_to_date` |
| X01-FR-009 | INT-04 | Every module PRD SHALL reference requirement IDs and AT IDs (traceability tables) | Should | P0 | PRD review |

## 5. Business rules & algorithms

- **Units (§8.2):** USD for target and cycle net; account currency labelled explicitly; DD percent 0–100; confidence/probability 0–1 with a `semantics` string; lot in broker lots; price in quote currency.
- **Money precision [PROPOSED]:** stored as `Decimal` at full precision; rounding only at presentation or at a broker boundary using the broker's step. No implicit float arithmetic.
- **Time:** all timestamps timezone-aware UTC. Broker server time travels as separate metadata where needed (M02).
- **IDs:** opaque strings `<prefix>-<uuid4 hex>`, immutable.
- **Control lifecycle (§9.1)** — legal transitions:
  `STOPPED → STARTING`; `STARTING → RECONCILING | SAFE_MODE`; `RECONCILING → RUNNING | STOPPED | SAFE_MODE | RISK_LOCKED`; `RUNNING → PAUSED | FINISHING | CLOSING | RISK_LOCKED | SAFE_MODE`; `PAUSED → RUNNING | FINISHING | CLOSING | RISK_LOCKED | SAFE_MODE`; `FINISHING → STOPPED | CLOSING | RISK_LOCKED | SAFE_MODE`; `CLOSING → STOPPED | RISK_LOCKED | SAFE_MODE`; `RISK_LOCKED → CLOSING | SAFE_MODE | RECONCILING` (the last only through approved recovery, enforced by M06); `SAFE_MODE → RECONCILING | CLOSING | RISK_LOCKED`.
- **Risk lock is a separate persisted dimension** (§9.1): the lifecycle may be `CLOSING` while `risk_locked = true`. The state object carries both; no transition clears the lock — only M06's approved-recovery path does.
- **Cycle lifecycle:** `IDLE → ACTIVE → CLOSING → CLOSED`; `ACTIVE → CLOSED` is illegal (close is never atomic, §4.1.4); `CLOSED` is terminal for that `cycle_id`.
- **New exposure** is allowed only in `RUNNING` and only with no risk lock (§9.3 fail-closed).

## 6. Data model & interfaces

| Contract | Producer | Consumer | Schema/version | Validity/errors |
|---|---|---|---|---|
| `EventEnvelope` | all | M08 journal, all | `schema_version="1.0"` | pydantic `ValidationError` on any rule break |
| 18 payloads (§8.2) | per catalog | per catalog | `docs/contracts/*.schema.json` | same |
| `ControlState`, `CycleState`, transition functions | X01 | M06, M08, M01 | code | `IllegalTransition` |

## 7. UX / control behavior

Not applicable (no UI). The dashboard consumes the JSON Schemas.

## 8. Non-functional requirements

Typed (mypy strict), deterministic tests, pinned dependencies (`uv.lock`), Python 3.13.

## 9. Failure modes & safety

| Failure | Detection | Safe behavior | Recovery | Test ID |
|---|---|---|---|---|
| Producer emits float money | validation | event rejected | fix producer | X01-FR-003 |
| Naive timestamp | validation | event rejected | fix producer | X01-FR-001 |
| Payload/type mismatch | validation | event rejected | fix producer | X01-FR-002 |
| Code and schema files drift | test | CI red | regenerate | X01-FR-008 |

## 10. Testing & acceptance criteria

- Given a `trade.intent` envelope with a `risk.decision` payload, when validated, then it is rejected.
- Given `requested_lot=0.01` as a float, when validated, then it is rejected; as `"0.01"` it is accepted as `Decimal`.
- Given `RUNNING`, when transitioning to `STARTING`, then `IllegalTransition`.
- Given `SAFE_MODE` with `risk_locked=true`, when moving to `RECONCILING`, then `risk_locked` stays true.

## 11. Implementation plan & deliverables

| Task ID | Output / path | Dependency | Acceptance |
|---|---|---|---|
| X01-T1 | `pyproject.toml`, `uv.lock`, tool config | — | `uv sync` clean |
| X01-T2 | `backend/app/core/{types,ids,time,enums,states,events}.py` | T1 | unit tests |
| X01-T3 | `backend/app/core/contracts.py` + `scripts/gen_schemas.py` → `docs/contracts/` | T2 | schema test |
| X01-T4 | CI jobs + `verify` | T1 | CI green |

## 12. Risks, open decisions & changes

| ID | Issue | Options | Impact | Decision owner | Status |
|---|---|---|---|---|---|
| X01-OD-1 | Inter-process transport | in-process + durable journal (MVP) vs broker | M08 topology | M08 | PROPOSED: in-process + SQLite journal |
| X01-OD-2 | EA bridge wire format (OD-01) | JSON over file/socket | MQL5 EA | M06 | TBD — no MT5 available (OD-12) |

## 13. Traceability & sign-off

| Master ID | Local FR | Test ID | Evidence | Status |
|---|---|---|---|---|
| INT-01 | X01-FR-001..004, 008 | `tests/contract/` | CI run on the PR | Implemented |
| INT-03 | X01-FR-005..007 | `tests/unit/core/test_states.py` | CI run on the PR | Implemented |
| INT-04 | X01-FR-009 | PRD tables | this file | Implemented |
| INT-02 | — | — | integration harness lands with X02 | Not started |
