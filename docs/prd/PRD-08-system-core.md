# PRD-08 — System Core

- Module ID: M08
- Owner: agent (Claude), under developer directive 2026-11-27
- Reviewers: M06 (state it persists), M01 (API it consumes), X01, X02
- Status: Implemented — slice A #8, slice B #9 (parent #7)
- Version: 0.1
- Parent: `docs/plans/master_planV1.md` (V1.0)
- Target phase: P1
- Requirement IDs: SYS-01, SYS-02 (local), SYS-03 (local storage), SYS-04 (safe-mode hooks), SYS-05 (local backup), SYS-08; UI-01 (API side); AT-20, AT-21, AT-22, AT-26

## 1. Summary & product goal

The plumbing every module shares: a durable local store with migrations, an idempotent event journal, versioned configuration that never holds plaintext secrets, a guarantee that only one executor trades a namespace, verified backups, health tracking, and an authenticated local API for the dashboard. Success = a restart, a duplicate event, a second executor, a stolen-cookie-free browser, or a restored backup cannot make the bot trade without the state M06 expects.

## 2. Scope

**MVP (this PRD):**
- **Slice A (#8):** SQLite (WAL) with versioned SQL migrations in `migrations/`; event journal (append-only, dedupe by `event_id`, global sequence, per-consumer offsets); config store (versioned, optimistic concurrency, secret *references* only, protected policy separate from user config); executor lease per namespace; backup with integrity verification and restore; health registry; structured JSON logs with secret redaction.
- **Slice B (#9):** FastAPI app per master plan §8.4; single-owner password auth with expiring bearer sessions; read endpoints over read models; redacted settings GET and versioned PATCH; five control commands recorded as `control.requested` events; command status; `/ws/live` with sequence numbers and resync; audit log; localhost bind by default.

**Advanced (not here):** VPS/Hybrid packaging, TLS termination, cloud storage/sync, signed updates, adaptive resource orchestration, scheduled backup cadence (SYS-05 tiers) — the mechanism exists, the scheduler arrives with the runtime wiring.

**Non-goals:** business decisions of M06 (M08 persists and transports, never overrides, §2.1); multi-user accounts.

**Assumptions [PROPOSED]:** single host, single Python process group sharing one SQLite file; stdlib `sqlite3` instead of SQLAlchemy (master plan §5.2 lists SQLAlchemy as PROPOSED; the store is small and the transaction boundaries are explicit, so an ORM adds surface without removing risk).

## 3. Actors & use cases

- **Owner** logs in from a browser on the same machine, reads status, changes settings, sends control commands. Closing the browser changes nothing (AT-20).
- **Modules** append events and read them from their offset after a crash (at-least-once).
- **Executor (M06)** acquires the namespace lease before any broker write and renews it; a second process is refused (AT-26).
- **Operator** takes a backup, verifies it, restores it; after restore M06 must reconcile against the broker before trading (AT-22).

## 4. Functional requirements

| Local ID | Master ID | Requirement | Priority | Phase | Acceptance test |
|---|---|---|---|---|---|
| M08-FR-001 | SYS-03 | SHALL apply numbered SQL migrations once, in order, transactionally | Must | MVP | `test_migrations_*` |
| M08-FR-002 | SYS-03 | Journal SHALL ignore a second append of the same `event_id` and report it | Must | MVP | `test_journal_dedupe` |
| M08-FR-003 | SYS-04 | Journal SHALL let a consumer resume from its last committed offset | Must | MVP | `test_consumer_offsets` |
| M08-FR-004 | SYS-08 | Config SHALL reject a plaintext secret; only `env:NAME` references | Must | MVP | `test_plaintext_secret_rejected` |
| M08-FR-005 | UI-05 | Config update SHALL require the expected version (conflict otherwise) | Must | MVP | `test_config_version_conflict` |
| M08-FR-006 | VAL-06, §8.5 | User config SHALL NOT change protected policy or enable Live | Must | MVP | `test_protected_fields_not_user_editable` |
| M08-FR-007 | SYS-02, AT-26 | Lease SHALL allow one holder per namespace; expired lease may be taken over | Must | MVP | `test_lease_*` |
| M08-FR-008 | SYS-05, AT-22 | Backup SHALL pass `integrity_check` and restore SHALL reproduce persisted state | Must | MVP | `test_backup_restore_*` |
| M08-FR-009 | SYS-04 | Health registry SHALL report a component stale after its heartbeat TTL | Must | MVP | `test_health_*` |
| M08-FR-010 | SYS-08 | Logs SHALL redact secret values and secret-like keys | Must | MVP | `test_log_redaction` |
| M08-FR-011 | SYS-08, AT-21 | API SHALL deny every non-public route without a valid session | Must | MVP | `test_api_auth_*` |
| M08-FR-012 | TRD-05, §8.4 | Control endpoints SHALL record `control.requested` + audit and return 202 with `command_id`, never a fill/close claim | Must | MVP | `test_control_*` |
| M08-FR-013 | UI-01, AT-20 | `/ws/live` SHALL stream journal events with sequence numbers and accept a resync cursor | Must | MVP | `test_ws_*` |
| M08-FR-014 | SYS-08 | Settings GET SHALL never return a secret value | Must | MVP | `test_settings_redacted` |
| M08-FR-015 | SYS-08 | Server SHALL bind 127.0.0.1 unless explicitly configured | Must | MVP | config default test |

## 5. Business rules & algorithms

- **Secrets:** a secret field holds a reference `env:<NAME>`; resolution reads the process environment at use time. Anything else in a `*_ref` field is rejected. Redaction masks the **whole value** (string, list, dict, model) under keys named like `api_key(s)`, `secret(s)`, `token(s)`, `password(s)`, `authorization`, `cookie(s)`, `credential(s)` (as whole `_`/`-` separated words, so `idempotency_id` or `monkey` stay readable), and every occurrence of a known secret value; unknown objects are logged as scrubbed text.
- **Config:** `trading.initial_lot`, `profit_target_usd`, `max_drawdown_percent` are `null` until the owner sets them (no ready-to-trade default, §8.5); `order_scope=BOT_ONLY` and `drawdown_scope=ACCOUNT_EQUITY` are fixed literals. `system.live_allowed` is protected policy, not user config. Each update writes a new immutable version row; M06 snapshots the version at cycle start (effective next cycle, §4.1 PROPOSED).
- **Lease:** row `(namespace, holder, expires_at)`; acquire = insert, or update when expired or same holder; renew extends; release deletes if holder matches. Limitation: binds processes sharing this SQLite file only — cross-host exclusivity (AT-26 Hybrid) needs shared storage (TBD, OD-10).
- **Transactions:** `BEGIN IMMEDIATE`; a nested call becomes a SAVEPOINT so store operations (flag + audit, append + offset) compose atomically; a failed COMMIT rolls back so the connection is never left mid-transaction. One connection per thread/request — never share one across concurrent threads.
- **Migrations:** split into complete statements with `sqlite3.complete_statement` (triggers and `;` in literals are safe); the applied-check runs under the write lock, so two processes migrating at once do not collide.
- **Journal:** only a repeated `event_id` is ignored (`ON CONFLICT(event_id) DO NOTHING`); any other constraint error raises. The table indexes `event_type`, `cycle_id`, `occurred_at`; the envelope JSON is the record. `read_raw_since` forwards JSON without re-validation (for `/ws/live`).
- **Backup:** SQLite online backup API → file (a consistent snapshot); verify by opening read-only via `Path.as_uri()` (paths with spaces, `#`, `?` are safe) and `PRAGMA integrity_check == ok`; a failed copy is deleted. Restore stages the backup in a temp file, writes `requires_reconcile` **into the staged image**, then copies it over the live DB — so no crash can leave restored state without the marker. M06 must clear it after broker reconciliation (§9.3 "rollback must not restore old state over broker reality").
- **Auth:** one owner; password hashed with `hashlib.scrypt` (n=2^14, r=8, p=1, 16-byte salt); login returns a random 32-byte token; only its SHA-256 is stored, with expiry (12 h [PROPOSED]); comparison in constant time; login attempts are *reserved* under a lock before the slow scrypt check (5 per minute [PROPOSED]), so parallel guesses cannot slip past the limit; a correct password frees its slot; a malformed stored hash fails closed; expired sessions are purged at login.
- **WebSocket `/ws/live`:** first message `{token, since}` within 5 s (close 4408 on timeout), validated strictly (close 4400 on bad input), session re-checked every poll (close 4401); SQLite work runs off the event loop; events are forwarded as stored JSON.
- **Status read model:** each field reports the journal `seq` it came from; the risk lock follows the newest of `control.completed`/`risk.updated`; health is the latest event **per component** with the same stale-after-TTL rule as the in-process registry, so a silent DOWN component never drops out.
- **Settings round-trip:** GET masks secret-named fields; PATCH maps a masked value back to the stored one (`unredact`), for any secret-named field.
- **Operator CLI:** `uv run python -m app.api set-password` (password read from the terminal) and `uv run python -m app.api serve`. Bearer header, no cookies → no CSRF surface; CORS limited to the configured dashboard origin.

## 6. Data model & interfaces

| Contract | Producer | Consumer | Schema/version | Validity/errors |
|---|---|---|---|---|
| `events` table | all via `Journal.append` | all | migration 0001 | duplicate `event_id` ignored |
| `consumer_offsets` | consumers | journal | 0001 | monotonic |
| `config_versions` | M08 API | M06, M01 | 0001 | version conflict → error |
| `executor_leases` | M06 | M08 | 0001 | held → refused |
| `audit_log`, `sessions`, `owner` | M08 API | M08 | 0001 | — |
| `system_flags` (`requires_reconcile`) | restore | M06 | 0001 | M06 clears |
| REST/WS | M08 | M01 | master plan §8.4 | 401/403/409/422 |

## 7. UX / control behavior

The API returns `as_of` timestamps and a `stale` flag on read models; a control request returns `{"command_id", "status": "ACCEPTED"}` meaning *recorded*, not executed. Live enablement has no endpoint.

## 8. Non-functional requirements

Localhost by default; no secrets in config files, logs or API responses; WAL mode; every write in a transaction; deterministic tests with a temp DB.

## 9. Failure modes & safety

| Failure | Detection | Safe behavior | Recovery | Test ID |
|---|---|---|---|---|
| Duplicate event delivery | unique `event_id` | ignored | — | M08-FR-002 |
| Process crash mid-consume | offset not committed | re-read from last offset | consumer dedupes | M08-FR-003 |
| Second executor started | lease held | refused, no broker writes | wait for expiry | AT-26 |
| Restored stale backup | `requires_reconcile` set | M06 must reconcile first | M06 clears flag | AT-22 |
| Component heartbeat stale | health registry | report DOWN → M06 SAFE_MODE | heartbeat resumes | M08-FR-009 |
| Brute-force login | rate limiter | 429 | window passes | AT-21 |

## 10. Testing & acceptance criteria

Given/When/Then per FR in `tests/unit/storage/`, `tests/unit/config/`, `tests/integration/api/`. Shared IDs: AT-20, AT-21, AT-22, AT-26 (single-host part).

## 11. Implementation plan & deliverables

| Task ID | Output / path | Dependency | Acceptance |
|---|---|---|---|
| M08-T1 | `migrations/0001_core.sql`, `backend/app/storage/db.py` | X01 | FR-001 |
| M08-T2 | `backend/app/storage/journal.py` | T1 | FR-002/003 |
| M08-T3 | `backend/app/core/config.py`, `backend/app/storage/config_store.py`, `backend/app/core/secrets.py` | T1 | FR-004..006 |
| M08-T4 | `backend/app/storage/lease.py`, `backup.py`; `backend/app/core/health.py`, `logging.py` | T1 | FR-007..010 |
| M08-T5 | `backend/app/api/` (slice B) | T1–T4 | FR-011..015 |

## 12. Risks, open decisions & changes

| ID | Issue | Options | Impact | Decision owner | Status |
|---|---|---|---|---|---|
| M08-OD-1 | Cross-host executor exclusivity | shared DB / broker-side check | Hybrid AT-26 | M08, M06 | TBD (OD-10) |
| M08-OD-2 | Remote access (VPS) | TLS + VPN | SYS-08 remote | M08 | TBD — local only now |
| M08-OD-3 | Backup cadence scheduler | runtime timers | SYS-05 tiers | M08 | mechanism done, cadence with runtime |

## 13. Traceability & sign-off

| Master ID | Local FR | Test ID | Evidence | Status |
|---|---|---|---|---|
| SYS-03 | FR-001..003 | `tests/unit/storage/` | CI on slice A PR | Implemented |
| SYS-08 | FR-004, 010, 011, 014, 015 | storage/config/api tests | CI on slice PRs | Implemented (local) |
| SYS-05 | FR-008 | backup tests | CI | Implemented (manual trigger) |
| AT-22 | FR-008 + reconcile flag | backup test | CI | Implemented (M06 side pending) |
| AT-26 | FR-007 | lease tests | CI | Single host only |
| AT-20/21 | FR-011..013 | api tests | CI on slice B PR | Implemented |
