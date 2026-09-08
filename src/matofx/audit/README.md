# `matofx.audit` — hash-chained signed audit log

Append-only audit log with per-record hash chaining and Ed25519 signing. The compliance backbone of the product.

## Why this module exists

Per §D21 / §I46 of the brief, v1.0 ships a **hash-chained, signed** audit log with an 18-field schema, per-session-per-record reads as the default visibility, and an admin "Cleanup activity" view.

The audit log is **load-bearing for trust** — customers in regulated industries treat it as evidence. Two consequences:

1. It cannot be silently lost. Every other module that does something audit-worthy (login, permission grant, job submission, approval, file access, licence event, config change) calls into this module rather than logging "to be audited" elsewhere.
2. It cannot be tampered with after the fact. Each record's hash incorporates the prior record's hash; the chain is signed with an Ed25519 key. Verification is a CLI subcommand (`matofx audit verify`).

## Public surface

```python
from matofx.audit import (
    RecordInput, ensure_chain_head, make_audit_writer, verify,
)
from matofx.config import load_settings
from matofx.db import make_engine, make_session_factory, session_scope

settings = load_settings()
engine = make_engine(settings.db)
factory = make_session_factory(engine)
writer = make_audit_writer(settings.audit)

async with session_scope(factory) as session:
    await ensure_chain_head(session)            # idempotent first-run
    record_id = await writer.record(session, RecordInput(
        actor="user:42", actor_type="user",
        action="login.success",
        resource_type="session", resource_id="s-1",
        outcome="success",
    ))
```

## The 18 fields (§D21)

| # | Field | Source |
|---|---|---|
| 1 | `timestamp` | caller (defaults to now-UTC) |
| 2 | `actor` | caller |
| 3 | `actor_type` | caller (`user` / `service` / `system` / `anonymous`) |
| 4 | `action` | caller (e.g. `"login.success"`, `"job.submit"`) |
| 5 | `resource_type` | caller |
| 6 | `resource_id` | caller (string-encoded) |
| 7 | `outcome` | caller (`success` / `failure` / `denied`) |
| 8 | `request_id` | caller (optional) |
| 9 | `session_id` | caller (optional) |
| 10 | `source_ip` | caller (optional) |
| 11 | `prior_hash` | computed (hash of previous record, or genesis) |
| 12 | `hash` | computed (SHA-256 of canonical bytes) |
| 13 | `signature` | computed (Ed25519 over `hash`; `null` when unsigned) |
| 14 | `key_id` | computed (truncated SHA-256 of public key) |
| 15 | `schema_version` | computed (1 for v1.0) |
| 16 | `payload` | caller (JSON) |
| 17 | `reason` | caller (optional plain English) |
| 18 | `correlation_id` | caller (optional) |

## Hard rules

- **No deletes, ever.** Even a corrupted record stays; an explanatory record follows.
- **No updates.** Corrections are new records that reference the prior one.
- **The signing key is not stored alongside the data** — it lives in the configured key source (file / KMS / TPM per §C16). Phase 0 ships file-backed keys; KMS / TPM land later.
- **Verification is the source of truth** — application code that "trusts" the chain without verifying is a bug.

## Defence-in-depth against direct DB tampering

The writer goes through `_lock_head` which serialises chain mutations through the singleton `audit_chain_head` row. Layers of defence beyond that:

- **Unique constraint on `prior_hash`** (migration `0007`) — a direct `INSERT` bypassing the writer can't introduce two records claiming the same prior link.
- **Unique constraint on `hash`** — duplicate record hashes are impossible at the DB level.
- **Chain-head missing guard** — if the `audit_chain_head` row is missing AND `audit_log` has records, the writer raises `ChainHeadMissingError` rather than silently fabricating a fresh chain. An attacker who deletes the head row to reset the chain can't write the next record.
- **Verifier loud about missing keys** — calling `verify` on a chain with signed records but no public keys returns failures (not silent OK). Operators can't accidentally pass an empty key set and conclude the chain is fine.

## Signing-key file permissions

`load_signing_key(path)` emits a `warnings.warn` on POSIX hosts when the keyfile is readable by anyone other than the owner — this is a private key and should be `chmod 600`. Suppress with `MATOFX_SUPPRESS_PERM_WARNING=1` (same env var as the config-file permission warning) on shared dev hosts.

## Chain serialisation

Concurrent writers are serialised through the singleton `audit_chain_head` row: each writer takes a `SELECT ... FOR UPDATE` lock on it before computing the new record's `prior_hash`. On Postgres this enforces a strict ordering across processes; on SQLite the writer-lock model already serialises writes, and the FOR UPDATE clause is a harmless no-op.

## Verification

`verify(session)` walks the chain (or any sub-range), recomputes each record's hash, checks links between records, and validates Ed25519 signatures when public keys are supplied. The CLI subcommand `matofx audit verify` exposes this end-to-end.

## Phase 0 status

- 18-field record schema (`schema.py`).
- Canonical hashing (`hashing.py`) — deterministic JSON form, fixed field order, bumpable schema version.
- Ed25519 keys (`keys.py`) — generate, load, key-id derivation; PEM file storage for v1.0.
- ORM models + migration (`orm.py`, `0002_audit_log.py`).
- Append API (`writer.py`) — chain-head locking, signing, transactional with caller's session.
- Verification (`verify.py`) — hash + chain link + signature checks; sub-range walks.
- CLI subcommand: `matofx audit verify [--start-id N] [--end-id N]`.
- Tests: hashing determinism, key round-trip, append-and-link, tamper detection (payload + chain), signed chain verification, sub-range walks, CLI smoke. SQLite is exercised in CI; Postgres tests run when `MATOFX_TEST_POSTGRES_URL` is set.
