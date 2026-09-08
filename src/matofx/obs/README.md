# `matofx.obs` — observability (logging, metrics, tracing)

Structured logging is shipped. Prometheus metrics, OpenTelemetry tracing, and three-tier health endpoints are deferred to Phase 2 (per §I46) and land alongside the FastAPI shell.

## What's shipped (Phase 0)

- **JSON-Lines logs** on stderr — one parseable JSON object per line, single stable schema.
- **`bind` / `current_context` / `clear_context`** — `contextvars`-backed request/user binding that propagates through `await` and stays isolated across concurrent `asyncio.Task`s.
- **`get_logger(name)`** — stdlib `logging.Logger` with the context filter pre-attached. The canonical entry point for every module.
- **`configure(settings, *, stream=None)`** — process-startup wiring; idempotent. Takes a `LogSettings`; the only function-signature default is `stream=None → sys.stderr` (an I/O contract, not customer-tunable behaviour).
- **Security baseline** — credential redaction, per-line size cap, process / host metadata. See below.

## What's deferred to Phase 2

- Prometheus `/metrics` endpoint with standard process + MATOFX-specific counters / histograms.
- OpenTelemetry instrumentation, off by default; opt-in via config.
- Three-tier health endpoints (`/healthz/live`, `/healthz/ready`, `/healthz/startup`).
- Admin "Errors" + "System health" pages (UI surfaces).

## Naming note

This package is `obs`, not `logging`, deliberately — `matofx.logging` would shadow the stdlib `logging` module and is a known Python footgun.

## JSON-Lines schema

Every record carries the following mandatory fields:

| Field | Type | Notes |
|---|---|---|
| `timestamp` | string | ISO-8601 UTC, microseconds, `Z` suffix |
| `level` | string | `"debug"` / `"info"` / `"warning"` / `"error"` / `"critical"` |
| `logger` | string | Logger name (typically `__name__` of the module) |
| `message` | string | Rendered log message |
| `process_id` | int | OS process id |
| `process_name` | string | Process name (`MainProcess` etc.) |
| `hostname` | string | Captured once at process start (omit via `LogSettings.include_hostname=False`) |

Optional, included when present:

- `request_id`, `user_id`, plus anything else bound via `bind(...)` — flattened to the top level.
- `exception` — formatted traceback when `logger.exception(...)` or `exc_info=True`.
- Any `extra={...}` keys from the caller — flattened to top level (collisions with reserved fields are nested under `extra`).
- `truncated_bytes` — present only when the line was replaced by a truncation marker (see below).

Reserved field names (the seven mandatory + `exception` + `truncated_bytes`) cannot be overwritten by user code; collisions are re-routed under `extra`.

## Security

### Credential redaction

Field names whose lower-cased form contains any of these substrings have their values replaced with `"***REDACTED***"`:

- `password`, `passwd`, `pwd`
- `secret`, `token`, `private_key`, `credential`
- `api_key`, `apikey`
- `authorization`, `cookie`

Redaction applies recursively to nested dicts and lists. Customers can extend the list via `LogSettings.redact_fields` (e.g. `["clearance_level", "customer_id"]` for site-specific terms). The built-in list always applies.

`session_id` is **deliberately NOT** redacted — audit records carry it as a meaningful, non-credential identifier.

### Per-line size cap

`LogSettings.max_line_bytes` (default 16384) caps the size of a single log line. A record that would serialise larger is replaced with a small truncation marker carrying the timestamp / level / logger / process metadata plus `truncated_bytes` (the original size in bytes) and a synthetic message that says it was truncated. Defends against:

- A single runaway record dumping megabytes into the operator's log pipeline.
- Accidental object-graph dumps via `extra`.
- Volume-DoS via crafted requests that produce huge log records.

The marker is itself valid JSON-Lines and stays well under the cap.

## Secrets-in-logs policy

**Never put any of the following in a log message or `extra` field — even with redaction enabled:**

- Plain-text passwords, password hashes, or password resets.
- Session tokens (raw — pass `session_id` if you need correlation).
- Raw API keys, OAuth tokens, JWTs.
- Private keys (signing keys, TLS keys, SSH keys).
- Customer file contents (CAD, simulation inputs, output payloads).
- Full PII: national ID numbers, credit card numbers, full home addresses.
- Anything covered by the customer's data-classification policy as Confidential or above.

The redaction filter is a **defence-in-depth** measure — assume it has already failed before you write the log line. Audit-grade events go through `matofx.audit`, not the log stream.

## Configuration

```toml
# matofx.toml
[log]
level = "INFO"
ensure_ascii = false
max_line_bytes = 16384
include_hostname = true

# Optional: extend the redaction list with site-specific terms.
redact_fields = ["clearance_level", "customer_id"]
```

## Where to start reading

- `config.py` — `configure(settings)` is the entry point.
- `formatter.py` — the JSON-Lines formatter, redaction, and truncation marker.
- `context.py` — `bind` / `current_context` / `clear_context`.
- `logger.py` — `get_logger` + the context filter.

## Phase 0 status

Logging primitives shipped, security-hardened, **44 tests** covering schema, context propagation, redaction (built-in + customer + recursive), per-line size cap, process / host metadata, settings round-trip. Metrics + tracing + health endpoints land in Phase 2 alongside the web shell.
