# `matofx.config` — configuration loader

Loads MATOFX's runtime configuration from a single TOML file plus environment-variable overrides, validates it, and exposes a typed, frozen `Settings` object that's the single source of truth for every later module.

## Why this module exists

Per §A2 / §A5 of the brief, the same artefact must run on:

- on-prem Windows / Linux,
- a customer-controlled cloud VM,
- a Funis-hosted single-customer VM,

with everything from a 5-user single-box install up to a split-out web / workers / DB / storage shape — **driven only by configuration, not code paths.**

Per §A1 (single-tenant per install), there is **one config per install** — no multi-tenant config switching at runtime.

## Public surface

```python
from matofx.config import load_settings, Settings

settings: Settings = load_settings()                # default lookup chain
settings = load_settings(Path("/etc/matofx/matofx.toml"))  # explicit
```

`Settings` is **frozen**: attributes can't be mutated after load. Sections:

- `Settings.environment` — required, one of `dev` / `test` / `staging` / `prod`. No silent default.
- `Settings.log` — `LogSettings` (level, redaction, size cap, hostname). See `obs/README.md`.
- `Settings.db` — `DbSettings` (dialect, URL as `SecretStr`, pool, timeouts).
- `Settings.audit` — `AuditSettings` (signing key path, require_signing).
- `Settings.auth` — `AuthSettings` (session timeouts, lockout, Argon2id parameters).
- `Settings.features` — `FeatureSettings` (per-customer feature flags).

## Lookup chain

When `load_settings()` is called without an explicit path:

1. `MATOFX_CONFIG` environment variable, if set.
2. OS-appropriate system path:
   - Linux / macOS: `/etc/matofx/matofx.toml`.
   - Windows: `%PROGRAMDATA%\matofx\matofx.toml`.
3. Per-user fallback (dev): `~/.config/matofx/matofx.toml` or `%APPDATA%\matofx\matofx.toml`.

If no file is found, defaults are used and env-var overrides still apply. Operators who want a hard error on missing config pass an explicit path.

## Env-var overrides

`MATOFX__SECTION__KEY=value` overrides the TOML, after the file load. Booleans accept `true` / `false` / `yes` / `no` / `on` / `off` / `1` / `0` (case-insensitive). Integers and floats are coerced. Examples:

```bash
MATOFX__ENVIRONMENT=prod
MATOFX__DB__URL=postgresql://u:p@db:5432/matofx
MATOFX__LOG__LEVEL=WARNING
MATOFX__AUDIT__REQUIRE_SIGNING=true
```

Env wins over the file.

## Security

### `db.url` is a `SecretStr`

Postgres URLs typically embed a password. `DbSettings.url` is wrapped in `pydantic.SecretStr` so it never appears in `repr()`, tracebacks, or `model_dump(mode="json")` output. Callers that need the raw string call `.get_secret_value()` once at the engine factory; nowhere else.

For ergonomic construction, `DbSettings.from_url("postgres://...", dialect="postgres")` wraps the string for you. Tests use this rather than wrapping `SecretStr(...)` at every site.

### URL scheme validation

`DbSettings.url` rejects values that don't start with one of: `sqlite:`, `sqlite+aiosqlite:`, `postgres:`, `postgresql:`, `postgresql+asyncpg:`. Catches the classic `sqlite:./x.db` typo (missing the third slash) at config-load time rather than first connect. The error message quotes only the **scheme prefix** so an embedded password isn't echoed back.

### Validation errors don't leak input values

Pydantic by default appends `input_value=...` to validation errors. The loader passes `include_input=False` when rendering errors so a DB URL with an embedded password isn't echoed to the operator's screen.

### Production environment cross-checks

When `environment == "prod"`, the model validator enforces:

- **Hard fail**: `audit.signing_key_path` must be set.
- **Hard fail**: `audit.require_signing` must be `True`.
- Soft warning (`warnings.warn`): `db.dialect == "sqlite"` (Postgres recommended above ~5 concurrent users per §B8).
- Soft warning: `auth.session_idle_timeout_minutes > 240` (over 4 hours).
- Soft warning: `auth.failed_login_lockout_threshold > 10`.

Soft warnings go to stderr by default; operators who want them as errors run with `python -W error`.

### POSIX file-permission warning

If the config file is readable by other users (`stat().st_mode & 0o077 != 0`), the loader emits a `warnings.warn` reminding the operator to `chmod 600`. Skipped on Windows (different ACL model). Suppress for shared dev hosts via `MATOFX_SUPPRESS_PERM_WARNING=1`.

The check is intentionally non-fatal — some legitimate environments rely on group-readable configs.

## Pattern: no-second-defaults

The `Settings` model is the **single source of fallback values**. No function elsewhere in the codebase has a `parameter=default_value` that mirrors a Pydantic default; functions take a settings section as a parameter. See `CONTRIBUTING.md` § "Hardcoded values — strict rule".

## Where to start reading

- `loader.py` — `load_settings(path: Path | None = None) -> Settings`. File discovery, env-var merge, validation, plain-English errors.
- `models.py` — `Settings` and the section sub-models. All cross-field validators (production safety) live here.
- `paths.py` — OS-specific default lookup paths.

## Phase 0 status

Shipped: full loader, all five sub-models, env-var override, frozen Settings, plain-English errors, security hardening (`SecretStr` URL, scheme validator, prod cross-checks, permission warning, error-output sanitisation). Customer-facing `etc/matofx.example.toml` is the canonical reference; `tests/config/test_loader.py::test_example_config_loads` keeps it from drifting.

55+ tests across `test_loader.py`, `test_models.py`, `test_paths.py`, `test_hardening.py`.
