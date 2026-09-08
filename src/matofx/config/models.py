"""Typed configuration model.

The single :class:`Settings` object is the run-time configuration of an
install. It is loaded once at process startup from a TOML file, optionally
overridden by environment variables, validated, frozen, and then passed
into anything that needs to know "how is this install configured?"

Every section is its own ``BaseModel`` so callers can pass just the slice
they care about and tests can construct minimal instances. The top-level
``Settings`` is frozen — at runtime, configuration is immutable; tests use
``Settings.model_copy(update=...)`` when they need a variant.

The shape here is the **Phase 0** subset — only what the foundation
modules (``obs``, ``db``, ``audit``, ``cli``) need. Sections for auth,
HPC, GPU, queue, file storage, notifications, etc. are added as those
modules land.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DbDialect = Literal["sqlite", "postgres"]
Environment = Literal["dev", "test", "staging", "prod"]

_VALID_DB_URL_PREFIXES: Final[tuple[str, ...]] = (
    "sqlite:",
    "sqlite+aiosqlite:",
    "postgres:",
    "postgresql:",
    "postgresql+asyncpg:",
)
"""Recognised URL schemes for :class:`DbSettings.url`.

The engine factory translates any of these into the async-driver form
SQLAlchemy needs. Other schemes are rejected at validation time —
catches typos like ``"sqlite:./x.db"`` (missing the third slash) at
config-load rather than first-connect.
"""

_PROD_LONG_IDLE_TIMEOUT_MINUTES: Final[int] = 240
_PROD_HIGH_LOCKOUT_THRESHOLD: Final[int] = 10


class _Frozen(BaseModel):
    """Base for all config sections — frozen, extra-fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class LogSettings(_Frozen):
    """Structured-logging configuration consumed by :mod:`matofx.obs`.

    Every value here is the single source of fallback for the
    structured logger's behaviour. The redaction list and per-line
    cap protect against accidental credential leaks and volume DoS.
    """

    level: LogLevel = "INFO"
    """Root logger level. ``"INFO"`` is right for steady-state production."""

    ensure_ascii: bool = False
    """When True, non-ASCII characters render as ``\\uXXXX`` escapes."""

    redact_fields: list[str] = Field(default_factory=list)
    """Customer-supplied **additional** field-name substrings to redact.

    The built-in deny list always applies (``password``, ``token``, ``secret``, etc.);
    this list extends it with site-specific terms like ``"customer_id"`` or
    ``"clearance_level"`` that the application catalog wouldn't know about.
    Matching is case-insensitive substring; values become ``"***REDACTED***"``.
    """

    max_line_bytes: Annotated[int, Field(ge=1024, le=1024 * 1024 * 16)] = 16_384
    """Per-line ceiling in bytes. Lines exceeding this are replaced with a
    short truncation marker (still valid JSON-Lines) so a runaway log
    record can't exhaust the operator's log pipeline."""

    include_hostname: bool = True
    """Whether to include a ``hostname`` field on every log record.

    Useful when the same log pipeline ingests output from multiple MATOFX
    instances. Hostname is captured once at process start.
    """


class DbSettings(_Frozen):
    """Database connection configuration consumed by :mod:`matofx.db`.

    The ``url`` accepts the friendly forms ``sqlite:///path`` and
    ``postgres://...`` / ``postgresql://...``. The engine factory in
    :mod:`matofx.db.engine` translates these to the async-driver URL
    SQLAlchemy needs (``sqlite+aiosqlite``, ``postgresql+asyncpg``).

    The URL is wrapped in :class:`pydantic.SecretStr` so it never
    appears in ``repr()``, tracebacks, or :meth:`model_dump` output —
    Postgres URLs typically embed a password and leaking that into a
    log line or error message would be a real credential disclosure.
    Callers that need the raw string call :meth:`SecretStr.get_secret_value`.
    """

    dialect: DbDialect = "sqlite"
    url: SecretStr = Field(default=SecretStr("sqlite:///./matofx.db"))

    pool_size: Annotated[int, Field(ge=1, le=200)] = 10
    """Postgres-only: steady-state pool size. Ignored for SQLite."""

    pool_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    """Postgres-only: how long callers wait for a connection from the pool
    before failing fast. Stops requests piling up behind a saturated DB."""

    max_overflow: Annotated[int, Field(ge=0, le=100)] = 10
    """Postgres-only: connections allowed temporarily above ``pool_size``
    during bursts. Set to 0 to disable bursting entirely."""

    pool_recycle_seconds: Annotated[int, Field(ge=60, le=86_400)] = 1_800
    """Postgres-only: recycle connections older than this. Defends against
    idle-cut firewalls (typical default ~30 min). The default of 1800
    matches that window."""

    statement_timeout_ms: Annotated[int, Field(ge=0)] = 30_000
    """Postgres-only: server-side hard ceiling on query duration (0 disables)."""

    sqlite_busy_timeout_ms: Annotated[int, Field(ge=0)] = 5_000
    """SQLite-only: how long SQLite waits before raising "database is locked"."""

    connect_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 10
    """Initial connect timeout (both dialects). Stops a misconfigured
    DB from hanging the application's startup forever."""

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> DbSettings:
        """Construct from a plain string URL — type-safe convenience.

        ``url`` is wrapped in :class:`SecretStr` so test code and
        callers that have a plain string (e.g. from environment) don't
        need to wrap it themselves. Equivalent to
        ``DbSettings(url=SecretStr(url), **kwargs)`` but mypy-friendly.
        """
        return cls(url=SecretStr(url), **kwargs)  # type: ignore[arg-type]

    @field_validator("url", mode="before")
    @classmethod
    def _coerce_url(cls, v: object) -> SecretStr:
        """Accept ``str`` or ``SecretStr`` at construction time.

        Pydantic 2 auto-coerces ``str`` → ``SecretStr`` at runtime, but
        mypy doesn't see that without configuration. This explicit
        before-validator makes the coercion visible at the type level
        and lets us reject everything else with a clear message.
        """
        if isinstance(v, SecretStr):
            return v
        if isinstance(v, str):
            return SecretStr(v)
        raise ValueError("Database URL must be a string")

    @field_validator("url")
    @classmethod
    def _url_validates(cls, v: SecretStr) -> SecretStr:
        """Reject empty URLs and URLs without a recognised scheme."""
        raw = v.get_secret_value()
        if not raw:
            raise ValueError("Database URL must not be empty")
        if not raw.startswith(_VALID_DB_URL_PREFIXES):
            # Quote only the scheme prefix so a user-supplied password
            # in the URL doesn't get echoed back into the error message.
            scheme_end = raw.find("://")
            prefix = raw[: scheme_end + 3] if scheme_end != -1 else raw[:32]
            raise ValueError(
                f"Database URL doesn't start with a recognised scheme (got {prefix!r}). "
                "Expected one of: sqlite:///path, sqlite+aiosqlite:///path, "
                "postgres://..., postgresql://..., postgresql+asyncpg://..."
            )
        return v


class AuditSettings(_Frozen):
    """Audit-log configuration consumed by :mod:`matofx.audit`."""

    signing_key_path: Path | None = None
    require_signing: bool = False


class ComplianceSettings(_Frozen):
    """Compliance-attestation configuration (ADR-0019).

    ``identity_file`` points at the Funis-issued install-identity file.
    It holds the report signing key, so it is read at signing time and
    never copied into the database — treat it like a private key on
    disk (owner-only permissions).

    ``issuer_public_keys`` (key id → hex Ed25519 public key) supplements
    the build-time trusted set in :mod:`matofx.compliance.issuer`. It
    exists so dev / test / self-hosted verification works without a
    release build; production installs should rely on the baked-in set.
    """

    identity_file: Path | None = None
    issuer_public_keys: dict[str, str] = Field(default_factory=dict)


class LicenceSettings(_Frozen):
    """Licence-subsystem configuration (brief §C16, ADR-0019 slice 2).

    ``file`` is where the signed licence manifest lives. The file is the
    authority and is re-verified on every load, so replacing it and
    running ``matofx licence reload`` is all a renewal takes.

    Trust for the issuer signature is shared with
    :class:`ComplianceSettings` — one supplier, one set of issuer keys —
    so there is no separate licence trust root to keep in step.
    """

    file: Path | None = None


SecretsBackend = Literal["memory", "encrypted_file"]
"""Which :class:`~matofx.secrets.SecretsStore` backend to construct."""


class SecretsSettings(_Frozen):
    """Secrets-store configuration consumed by :mod:`matofx.secrets`.

    The store resolves an opaque ``secret_ref`` (held on AOS-host / HPC-
    cluster rows) into the credential bytes the execution targets use to
    connect. Two backends:

    - ``memory`` (default) — :class:`~matofx.secrets.InMemorySecretsStore`;
      no persistence. Fine for dev / test and **local-only** installs that
      register no AOS/HPC hosts.
    - ``encrypted_file`` — :class:`~matofx.secrets.EncryptedFileSecretsStore`;
      a Fernet-encrypted file. Requires both ``file_path`` and
      ``master_key``. Generate the key once with ``matofx secrets init``.

    ``master_key`` is :class:`SecretStr` so it never appears in repr / log
    output. ``file_path``, when relative, resolves against the
    ``matofx.toml`` file's own directory (like the other path settings).
    """

    backend: SecretsBackend = "memory"
    file_path: Path | None = None
    master_key: SecretStr | None = None

    @model_validator(mode="after")
    def _encrypted_file_needs_key_and_path(self) -> SecretsSettings:
        """Reject ``encrypted_file`` without both a path and a key."""
        if self.backend == "encrypted_file":
            missing = [
                name
                for name, value in (("file_path", self.file_path), ("master_key", self.master_key))
                if value is None
            ]
            if missing:
                raise ValueError(
                    "secrets.backend = 'encrypted_file' requires "
                    f"{' and '.join(missing)} to be set. Generate a key with "
                    "`matofx secrets init` and point file_path at a writable location."
                )
        return self


class FeatureSettings(_Frozen):
    """Customer-toggleable feature flags consumed by :mod:`matofx.features`.

    The list of *known* feature slugs is a closed catalog defined in
    :class:`matofx.features.Feature`. Slugs in ``enabled`` that aren't
    in the catalog are rejected at validation time with a plain-English
    message — preserves the "code defines features, customers enable
    them" model.

    In production, the signed licence manifest is the authoritative
    source; this model carries the loaded view of that manifest. For
    dev / test, the ``[features]`` section in ``matofx.toml`` populates
    it directly.
    """

    enabled: list[str] = Field(default_factory=list)
    """Slugs of features that should evaluate to ``feature_enabled() == True``."""

    @field_validator("enabled")
    @classmethod
    def _slugs_in_catalog(cls, v: list[str]) -> list[str]:
        # Lazy import: matofx.features imports FeatureSettings via TYPE_CHECKING
        # so we avoid a circular import at module load time.
        from matofx.features import Feature  # noqa: PLC0415

        catalog = {f.value for f in Feature}
        unknown = sorted(slug for slug in v if slug not in catalog)
        if unknown:
            raise ValueError(
                f"Unknown feature slug(s): {', '.join(unknown)}. "
                "Features are defined by the application code; ask your "
                "supplier to ship a release that introduces them."
            )
        # Deduplicate while preserving order.
        seen: set[str] = set()
        result: list[str] = []
        for slug in v:
            if slug not in seen:
                seen.add(slug)
                result.append(slug)
        return result


class EmailSettings(_Frozen):
    """SMTP + sender configuration for :mod:`matofx.notify`.

    When ``enabled`` is ``False`` (the default), the notifier short-
    circuits every send — ideal for headless dev / test installs and
    customers who don't want email at all. Production turns it on and
    points the SMTP fields at a real relay.

    ``smtp_password`` is :class:`SecretStr` so it never appears in
    repr / log output. ``public_url`` is the externally-visible base
    URL embedded in deep links — operators set it to the URL their
    users actually browse to.
    """

    enabled: bool = False
    """Master switch. ``False`` skips every send; the rest of the
    settings then act as documentation / Phase-2 prep."""

    smtp_host: str = "localhost"
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 25
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    use_tls: bool = False
    """Use implicit TLS (SMTPS, port 465). Use ``use_starttls`` for
    opportunistic upgrade on plain SMTP."""

    use_starttls: bool = True
    """Issue STARTTLS after connecting on plain SMTP. Recommended."""

    tls_verify: bool = True
    """Verify the SMTP server's TLS certificate. **Leave True.**

    Set ``False`` ONLY as a last-resort escape hatch on a host whose
    outbound TLS is intercepted by an antivirus / corporate proxy that
    injects a CA certificate OpenSSL 3.x rejects (the classic symptom
    is ``SSLCertVerificationError: ... Basic Constraints of CA cert
    not marked critical``). When ``False`` the notifier disables peer-
    certificate and hostname checks for the SMTP TLS layer only —
    credentials still travel, but a man-in-the-middle could read them,
    so this is acceptable for dev / demo boxes, never production.

    Deliberately TOML-only (like ``use_tls``): there is no General-
    Settings catalog row, so an admin cannot silently downgrade
    transport security from the web UI — it requires config-file
    access and a service restart."""

    timeout_seconds: Annotated[float, Field(ge=1.0, le=300.0)] = 30.0

    from_address: str = "matofx@localhost"
    """``From:`` header on every notification. Many relays require
    this match an authorised sender for the SMTP credentials supplied."""

    public_url: str = "http://localhost:8000"
    """Base URL embedded in deep links — e.g. ``https://matofx.example.com``.
    Trailing slashes are stripped on use."""


LlmProvider = Literal["disabled", "openai_compatible", "azure_openai", "bedrock", "ollama"]


class LlmSettings(_Frozen):
    """LLM endpoint wiring for auto-summary enrichment (auto-summaries.md §5.1).

    ``provider == "disabled"`` (the default) keeps the whole ``matofx.llm`` path
    inert and never imported at runtime — the templated baseline is unaffected.
    Sensitive wiring lives here in TOML, not the admin UI (mirrors the
    ``[email]`` precedent + ``feedback_email_tls_verify_flag``): an admin cannot
    point summaries at an external endpoint from the web UI; it needs config-file
    access + a restart, *and* the separate ``llm_summaries_enabled`` runtime
    setting before any prompt leaves the box.

    ``temperature`` defaults to ``0.0`` — determinism is treated as part of the
    anti-hallucination guard stack, not a style choice.
    """

    provider: LlmProvider = "disabled"
    base_url: str | None = None
    """OpenAI-compatible / Azure / Ollama endpoint base, e.g.
    ``https://gateway.internal/v1``. Required for those providers."""

    model: str = ""
    """Model id (or Azure deployment name / Bedrock model id)."""

    api_key: SecretStr | None = None
    """Bearer credential. Omit for keyless local endpoints (Ollama)."""

    api_version: str | None = None
    """Azure OpenAI ``api-version`` query parameter (Azure only)."""

    region: str | None = None
    """AWS region for the Bedrock runtime (Bedrock only); credentials come
    from the standard AWS chain / instance role, not ``api_key``."""

    timeout_seconds: Annotated[float, Field(ge=1.0, le=300.0)] = 20.0
    max_output_tokens: Annotated[int, Field(ge=1, le=8192)] = 600
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] = 0.0

    @model_validator(mode="after")
    def _check_provider_wiring(self) -> LlmSettings:
        """Plain-English validation per provider (no-op when disabled)."""
        if self.provider == "disabled":
            return self
        if not self.model:
            raise ValueError(
                f"llm.model is required when llm.provider='{self.provider}'. "
                "Set it to the model id (or Azure deployment / Bedrock model id)."
            )
        if self.provider in ("openai_compatible", "ollama") and not self.base_url:
            raise ValueError(
                f"llm.base_url is required when llm.provider='{self.provider}'. "
                "Set it to the endpoint base, e.g. 'https://gateway.internal/v1'."
            )
        if self.provider == "azure_openai" and (not self.base_url or not self.api_version):
            raise ValueError(
                "llm.base_url and llm.api_version are both required when "
                "llm.provider='azure_openai'."
            )
        if self.provider == "bedrock" and not self.region:
            raise ValueError(
                "llm.region is required when llm.provider='bedrock' (e.g. 'eu-west-1')."
            )
        return self


class AuthSettings(_Frozen):
    """User authentication and session settings consumed by :mod:`matofx.users`.

    The Argon2id parameters default to argon2-cffi's recommended values
    (m=64 MiB, t=3, p=4); operators on slower or busier hardware can
    tune them down. The MFA / SSO / password-policy fields land in
    Phase 2 along with the SSO provider and the password-reset flow.
    """

    session_idle_timeout_minutes: Annotated[int, Field(ge=1, le=1440)] = 30
    session_hard_timeout_hours: Annotated[int, Field(ge=1, le=720)] = 12
    password_min_length: Annotated[int, Field(ge=8, le=256)] = 12
    failed_login_lockout_threshold: Annotated[int, Field(ge=1, le=100)] = 5
    failed_login_lockout_minutes: Annotated[int, Field(ge=1, le=1440)] = 15
    argon2_time_cost: Annotated[int, Field(ge=1, le=20)] = 3
    argon2_memory_cost_kib: Annotated[int, Field(ge=8192, le=4194304)] = 65536
    argon2_parallelism: Annotated[int, Field(ge=1, le=16)] = 4

    # --- per-IP login throttle (defence in depth over per-account lockout) ---
    # Per-account lockout (above) stops a brute force against one username;
    # this caps total *failed* logins from one source IP across all
    # usernames, so an attacker can't spray guesses across many accounts.
    # In-process state (single-worker daemon) — the per-account lockout
    # remains the durable, cross-process guarantee.
    login_ip_rate_limit_max_failures: Annotated[int, Field(ge=1, le=10000)] = 30
    """Failed logins from one IP within the window before that IP is blocked."""
    login_ip_rate_limit_window_minutes: Annotated[int, Field(ge=1, le=1440)] = 15
    """Sliding window (minutes) over which failed logins are counted per IP."""
    login_ip_rate_limit_block_minutes: Annotated[int, Field(ge=1, le=1440)] = 15
    """Cooldown (minutes) an IP is blocked for once it trips the limit."""

    # --- browser session cookie (NiceGUI app.storage.user) ---
    # The UI keeps the bearer token in app.storage.user, keyed by a cookie
    # signed with this secret. Pin it in production so sessions survive an
    # API restart (an unset secret is randomised per process => everyone is
    # logged out on restart and signatures don't validate across workers).
    ui_session_secret: SecretStr | None = None
    """Stable secret signing the UI session cookie. ``None`` => ephemeral
    per-process secret (dev only; logs everyone out on restart)."""
    ui_session_cookie_secure: bool = False
    """Set the cookie ``Secure`` flag (HTTPS-only). Turn on in any
    deployment served over TLS; left off so plain-HTTP local dev works."""
    ui_session_cookie_same_site: Literal["lax", "strict", "none"] = "lax"
    """Cookie ``SameSite`` policy. ``lax`` suits the single-origin shell;
    ``strict`` is tighter, ``none`` (requires ``secure``) only for cross-site embeds."""


class ToolsSettings(_Frozen):
    """Tool registry + bundle storage configuration.

    ``bundles_dir`` is the local-filesystem directory the API server
    stages uploaded tool bundles into. Each tool gets its own
    ``<bundles_dir>/<alias>/`` subdirectory containing the descriptor
    file plus the tool's static files. The tool row's
    ``bundle_path`` column captures the absolute path so the runner
    knows where to copy from at job-stage time.

    The default ``./tools`` is appropriate for dev installs; production
    installs typically point this at a writable shared mount under
    ``/var/lib/matofx/tools`` so uploads survive container restarts.
    The directory is created on demand the first time a tool bundle
    is staged — operators don't need to mkdir it ahead of time.

    Relative paths (the default included) are resolved against the
    ``matofx.toml`` file's own directory — see
    :func:`matofx.config.loader.load_settings`. Operators who want a
    different location should set an absolute path here.
    """

    bundles_dir: Path = Path("./tools")
    """Where the API stages uploaded tool bundles. Created on demand.

    Relative values resolve against the config file's directory."""

    max_upload_bytes: Annotated[int, Field(ge=1, le=1024 * 1024 * 1024)] = 256 * 1024 * 1024
    """Per-bundle upload cap (default 256 MiB). Stops a runaway upload
    from filling the API host's disk."""


class WorkflowsSettings(_Frozen):
    """Admin-workflow icon storage.

    Workflows themselves don't ship a bundle the way tools do — they're
    pure metadata + an ordered list of member tool aliases. Their
    optional display icon does need somewhere to live on disk, though,
    so admins can upload a PNG that surfaces in the Workflow Quick
    Access card on the home page.

    ``icons_dir`` is the local-filesystem directory the API server
    writes uploaded workflow icons into. Each workflow gets a single
    ``<icons_dir>/<alias>.png`` file (replaced in-place on re-upload).
    Like ``tools.bundles_dir``, relative paths resolve against the
    config file's directory and the folder is created on demand the
    first time an icon is staged.
    """

    icons_dir: Path = Path("./workflow_icons")
    """Where the API stages uploaded workflow icon PNGs. Created on demand.

    Relative values resolve against the config file's directory."""

    max_upload_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)] = 8 * 1024 * 1024
    """Per-icon upload cap (default 8 MiB). Icons should be tiny; the
    cap stops a misuploaded photo from filling the disk."""


class TutorialsSettings(_Frozen):
    """Tutorial upload storage + size cap.

    Tutorials (PDFs + videos) staged via the Admin → Tutorials page
    land under :attr:`tutorials_dir`. Like the tools bundles dir, the
    default points at a local relative path that's appropriate for
    dev installs; production deployments typically aim this at a
    shared mount so uploads survive container restarts.

    Relative paths resolve against the ``matofx.toml`` file's own
    directory — see :func:`matofx.config.loader.load_settings`.
    """

    tutorials_dir: Path = Path("./tutorials")
    """Where the API stages uploaded tutorial files. Created on demand.

    Relative values resolve against the config file's directory."""

    max_upload_bytes: Annotated[int, Field(ge=1, le=4 * 1024 * 1024 * 1024)] = 512 * 1024 * 1024
    """Per-tutorial upload cap (default 512 MiB) — generous since
    short training videos are typically 50-300 MiB."""


class ModelsSettings(_Frozen):
    """Surrogate Model registry bundle storage (ADR-0008 §5.3).

    Fitted surrogate models persist as governed, sha256-verified bundles
    under ``<models_dir>/models/<environment>/model_<id>/v<ordinal>/``, and
    surrogate-as-tool exports stage their orchestrator under
    ``<models_dir>/exported_tools/<environment>/<alias>/``. The default
    ``./models`` suits dev installs; production points this at a writable
    shared mount so bundles survive container restarts. Relative paths
    resolve against the ``matofx.toml`` file's directory (like the other
    storage roots); the directory is created on demand.
    """

    models_dir: Path = Path("./models")
    """Root for surrogate-model bundles + exported-tool orchestrators.

    Relative values resolve against the config file's directory."""


class CoverageSettings(_Frozen):
    """Design-space coverage thresholds (ADR-0020 §16).

    The single home for every tunable the coverage engine reads —
    ``matofx.studies.coverage`` hardcodes no numbers and takes a
    :class:`CoverageSettings` on each entry point
    (``feedback_no_hardcoded_defaults_outside_config``). Defaults are
    deliberately forgiving: the modal case is a customer with a few dozen
    scattered ad-hoc jobs, and a floor tuned for textbook designs would
    answer ``insufficient`` to the people who most need an answer.
    """

    min_points_for_analysis: Annotated[int, Field(ge=3, le=1000)] = 6
    """Below this many training points the verdict is ``insufficient`` and no
    metric is computed — "we cannot tell" is a distinct answer from "we can
    tell, and it is bad"."""

    cold_start_ratio: Annotated[float, Field(ge=1.0, le=100.0)] = 5.0
    """Points per active dimension below which a response falls back to the
    model-free criterion: fewer than this and the GP's ARD length scales are
    steering by a compass they calibrated with the compass (slice 2)."""

    duplicate_eps: Annotated[float, Field(gt=0.0, lt=0.5)] = 1e-3
    """Normalised distance within which two points count as near-duplicates.
    Applied to **continuous axes only** — on an integer lattice of five values
    the normalised spacing is 0.25, and any generous epsilon would flag
    genuinely distinct adjacent ply counts."""

    min_level_points: Annotated[int, Field(ge=1, le=10_000)] = 3
    """Runs below which a categorical level is called underpopulated. The
    40-CFRP / 2-GFRP failure is silent on every continuous axis, so this is a
    first-class verdict input rather than a footnote."""

    min_points_per_group: Annotated[int, Field(ge=1, le=10_000)] = 8
    """Points per categorical group below which the analysis pools instead of
    splitting. Three selects of three levels is 27 groups; a reasonable 40-run
    design becomes 1.5 runs per group and *every* group would read ``poor``."""

    categorical_mismatch_distance: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    """Gower-style distance a categorical mismatch contributes in pooled
    mode."""

    max_abs_correlation: Annotated[float, Field(ge=0.5, lt=1.0)] = 0.85
    """Absolute Pearson correlation above which factors are called confounded.
    Reported independently of the sparsity verdict: a design perfectly
    space-filling *along a diagonal* is sparse-good and confounded-bad, and
    collapsing the two would let it pass as healthy."""

    verdict_thresholds: dict[str, float] = Field(
        default_factory=lambda: {"good": 0.70, "thin": 0.45}
    )
    """``coverage_score`` floors for ``good`` and ``thin``; below ``thin`` is
    ``poor``. Both keys required, ``0 < thin < good < 1``."""

    standard_runs_per_input: Annotated[int, Field(ge=2, le=500)] = 30
    """The **fixed** yardstick coverage is scored against, in runs per input.

    ADR-0020 §4.4. The score used to be measured against an evenly spread
    design of *the archive's own size*, which makes it an efficiency -- "are
    these runs arranged well for how many there are?" -- rather than a
    coverage measure. Two things followed, and both reached users:

    - **adding runs could make the verdict worse.** The gap shrank while the
      bar rose faster, so the score fell. Demonstrated on nested point sets:
      1.000 -> 0.941 -> 1.000 -> 0.978 -> 1.000 as runs were added.
    - it flattered wide designs, because in six dimensions the reference is
      itself poor and the ratio of two poor numbers is a good one.

    Pinned to a standard that depends only on the dimension, the score moves
    only when the radius does -- and a covering radius is non-increasing as
    points are added. Monotonicity becomes a property of the arithmetic
    rather than something to hope for.

    **30 is measured, not chosen** (``checks/coverage/calibrate_standard.py``,
    the §3 F7 discipline). Note first what the standard can and cannot do:
    ``score = bar / radius`` and the bar is the same for every design, so
    changing it rescales *every* score by one factor. It cannot improve
    separation -- that is fixed by the radii -- it decides **where the floors
    fall** across the distribution. So the requirement is placement:

    - a well-designed archive must never read ``poor``;
    - a badly-designed one must read ``poor``, or it cannot set the light.

    Seven design families (Latin hypercube, uniform, full factorial;
    one-factor-at-a-time, clustered, diagonal ridge, two-level corners) at
    four sizes with eight replicates each, sweeping the standard from 8 to 60.
    At 30: worst good 0.529, best bad 0.368 -- the 0.45 floor sits between
    them with a balanced +0.079 / -0.082 margin, and no floor is moved."""

    max_discriminating_axes: Annotated[int, Field(ge=2, le=64)] = 6
    """Varying inputs above which the **joint** score stops setting the light.

    A covering radius over the whole space cannot tell a good design from a
    bad one once the space is wide enough -- every design leaves enormous
    holes, so the readings converge. Measured at 20 runs per input, the
    margin between a Latin hypercube and the worse of one-factor-at-a-time /
    clustered:

    ``d=2: 0.47  d=3: 0.39  d=4: 0.38  d=5: 0.39  d=6: 0.34``
    ``d=7: 0.23  d=8: 0.21  d=9: 0.19``
    (margin = Latin hypercube score minus the worse of one-factor-at-a-time /
    clustered, at 20 runs per input)

    It holds to six and collapses at seven. Above the boundary a clustered
    design scores 0.79 -- comfortably ``good`` -- so letting the joint number
    drive the traffic light there would not be conservative, it would be
    **wrong in the flattering direction**. It is still reported, with the
    caveat, and the pairwise pass (§4.6) carries the verdict instead: two
    dimensions always have enough points to judge, whatever the tool's width."""

    reference_replicates: Annotated[int, Field(ge=1, le=99)] = 15
    """Ideal layouts the reference radius is the median of (§4.4).

    The reference is a Latin hypercube, and an LHS's own covering radius is a
    random variable -- its axes are permuted independently, so they can align
    badly. Taken from a single draw the bar was visibly noisy: sweeping the
    standard it read 0.3283 at ten runs per input and 0.2443 at twelve, a
    *larger* ideal design giving a 26% smaller bar. Every score is divided by
    this number, so that noise landed on the verdict.

    Affordable because the reference depends only on the envelope's **shape**
    and a point count -- never on the archive -- so it is memoised. A whole
    pairwise sweep needs two or three distinct layouts, and every option in a
    suggestion plan reuses the one bar."""

    pair_n_candidates: Annotated[int, Field(ge=256, le=100_000)] = 1024
    """Candidate points a **pairwise** covering radius is probed over (§4.6).

    Far fewer are needed than for the joint radius: 1024 candidates is 32 per
    axis in two dimensions, where the joint pass gets two per axis at twelve.
    The pairwise numbers are measurements, not estimates."""

    min_points_per_pair: Annotated[int, Field(ge=4, le=10_000)] = 20
    """Runs placeable on both axes below which a pair is not judged at all.

    "We cannot tell" is a distinct answer from "we can tell, and it is bad" —
    a two-point projection would report the second while meaning the first."""

    max_pair_axes: Annotated[int, Field(ge=2, le=40)] = 10
    """Axes entering the pairwise sweep; ten is forty-five pairs.

    Capped on **axes** rather than pairs so the sweep stays a complete sweep
    of whatever it covers. When it bites, the axes kept are the weakest on
    their own — a thin axis is the likelier half of a thin pair — and the
    report says how many pairs went unexamined rather than presenting a
    partial sweep as a full one."""

    one_at_a_time_floor: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.45
    """Pairwise score below which *every* pair must fall to call an archive
    one-factor-at-a-time.

    A distinct diagnosis, not a louder warning: a cross needs a designed set
    varying several inputs together, where one bad corner needs a handful of
    gap-filling runs."""

    min_discriminating_runs_per_input: Annotated[int, Field(ge=1, le=1_000)] = 20
    """Runs per varying input below which the figures cannot order designs.

    The **density** counterpart of :attr:`max_discriminating_axes`, which
    guards width alone. Measured the same way (unit-cube envelope, the same
    seven design families), worst well-designed archive against best
    badly-designed one:

    ``runs per input:   5      10     20     50``
    ``d=3 worst-good:   0.475  0.613  0.757  1.000``
    ``     best-bad:    0.503  0.503  0.503  0.503``
    ``d=4 worst-good:   0.577  0.635  0.850  1.000``
    ``     best-bad:    0.562  0.562  0.562  0.562``

    Below roughly 20 the margin is inside the noise and at 5 it inverts.
    Matches ``SUFFICIENT_DENSITY`` in ``checks/coverage/calibrate_standard``,
    whose own comment reads "runs per input at or above which a well-arranged
    archive should read good; below it, thin data is a true finding, not a
    false alarm."

    **This does not stand the verdict down, and must not.** The width blind
    spot is *flattering* — above six inputs a cluster scores 0.79 and reads
    ``good`` — so there the joint figure stops setting the light. The density
    blind spot is *conservative*: at six inputs or fewer no badly-designed
    archive ever reads ``good`` at any density, because best-bad plateaus in
    the 0.50-0.65 band. A hypercube and a cluster both read ``thin``, which
    is uninformative but never overclaims, and "thin" remains true of a small
    archive whatever its shape. Replacing it with "we cannot tell" would
    discard a correct finding.

    What it *does* gate is any claim about the **arrangement** — see
    :func:`matofx.ui._coverage_panel.arrangement_note`, which said "the runs
    themselves are well arranged, what is missing is runs" about a tight
    cluster scoring 0.957, higher than the clean hypercube it was compared
    against."""

    min_discriminating_pair_runs: Annotated[int, Field(ge=2, le=10_000)] = 60
    """Runs on a pair below which the pairwise pass stops setting the light.

    The density counterpart of :attr:`max_discriminating_axes`, and it was
    missing. That attribute's own docstring hands the verdict to the pairwise
    pass above six inputs on the grounds that "two dimensions always have
    enough points to judge, whatever the tool's width" — true about the
    *width*, and false about the *count*.

    Measured the way the standard was (``checks/coverage/calibrate_standard``
    methodology: unit-cube envelope, the same seven design families, four
    densities). Worst well-designed pair against best badly-designed pair,
    both readings:

    ``runs on the pair:   20      40      60      80      150+``
    ``density  worst-good: 0.30   0.39    0.53    0.66    0.88``
    ``         best-bad:   0.37   0.37    0.37    0.37    0.37``
    ``spread   worst-good: 0.49   0.51    0.53    0.63    0.62``
    ``         best-bad:   0.60   0.48    0.37    0.35    0.26``

    Below 60 the two overlap, and at 20 they invert — a Latin hypercube scores
    *worse* than a cluster. A criterion that cannot order the two must not set
    a traffic light, and the consequence of letting it was concrete: a clean
    20-run hypercube produced a ``poor`` pair in most draws, forcing the whole
    cohort to "Poorly covered" on a definition ("an entire two-input face was
    never explored") that was untrue of it.

    Note this is a floor on **runs placed on that pair**, not on the cohort:
    a run missing one of the two inputs is not evidence about them together.
    """

    n_candidates: Annotated[int, Field(ge=256, le=1_000_000)] = 4096
    """Candidate points the covering radius is evaluated over. A full grid
    explodes past ~4 factors, so the envelope is sampled instead."""

    min_candidate_resolution: Annotated[float, Field(gt=1.0, le=64.0)] = 5.5
    """Candidates-per-axis below which the overall figure is called a rough
    estimate rather than a measurement.

    **Set from measurement, not intuition.** The sampled radius is
    *low-biased* — the flattering direction, since the archive then looks
    better covered than it is. Against a 64x denser probe, six designs per
    dimension across uniform, clustered and Latin-hypercube shapes, the median
    error runs -1.4% at 2 factors, -3.4% at 3, -3.8% at 4, then **-6.5% at 5,
    -5.7% at 6, -8.1% at 7 and -9.2% at 8**.

    The previous value of 3.0 warned only from **8** factors up, so five, six
    and seven varying inputs produced a precise-looking number 6-8% optimistic
    with no caveat. 5.5 fires from 5 factors up, which is where the evidence
    puts the boundary."""

    edge_percentile: Annotated[float, Field(gt=50.0, le=100.0)] = 99.0
    """Percentile of sigma **at the training points** above which a prediction
    reads ``edge``. Computed once at fit and stored — never recomputed per
    predict, which would cost a training-set evaluation on every call and is
    impossible when ``keep_training_data`` is off.

    The default is high on purpose. The threshold is a percentile *of the
    training distribution*, so a value of ``p`` means ``100 - p`` per cent of
    the training runs sit above it **by construction** — and a GP interpolates
    its own data, so those sigmas are nearly identical and which side of the
    line a run falls on is arbitrary. At 90 that meant predicting at a run you
    had already done could report "the model is less certain here than usual",
    which is the opposite of true. At 99 ``edge`` means what it should:
    further from the runs than almost all of them are from each other."""

    max_lattice_cardinality: Annotated[int, Field(ge=2, le=100_000)] = 20
    """Integer-lattice size at or below which an axis is measured by
    **occupancy** rather than distance. An envelope of ``[1,5]`` with all five
    values run is completely covered; a mesh seed over ``[1,1000]`` is dense
    enough to behave continuously."""

    @field_validator("verdict_thresholds")
    @classmethod
    def _ordered_thresholds(cls, v: dict[str, float]) -> dict[str, float]:
        missing = sorted({"good", "thin"} - set(v))
        if missing:
            raise ValueError(
                f"verdict_thresholds needs both 'good' and 'thin' (missing: {', '.join(missing)})"
            )
        good, thin = float(v["good"]), float(v["thin"])
        if not 0.0 < thin < good < 1.0:
            raise ValueError(
                "verdict_thresholds must satisfy 0 < thin < good < 1 "
                f"(got thin={thin}, good={good})"
            )
        return {"good": good, "thin": thin}


class BrandingSettings(_Frozen):
    """Storage location and upload caps for Admin → General Settings images.

    The Branding & UX section of the General Settings catalog stores
    a header icon and a favicon. Each upload lands at
    ``<icons_dir>/<key>.<ext>`` where ``key`` is the catalog key
    (e.g. ``header_icon_path``) and ``ext`` is derived from the
    upload's mime type. The DB row records just the basename so the
    install stays portable.

    Like the other upload roots, ``icons_dir`` resolves relative
    paths against the ``matofx.toml`` file's own directory. The
    directory is created on demand the first time an admin saves
    an image — operators don't need to mkdir it ahead of time.
    """

    icons_dir: Path = Path("./branding")
    """Where the API stages uploaded branding images. Created on demand.

    Relative values resolve against the config file's directory."""

    favicon_max_upload_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)] = 2 * 1024 * 1024
    """Favicon upload cap (default 2 MiB). Real favicons are kilobytes;
    the cap stops a misuploaded photo from filling disk."""

    header_icon_max_upload_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)] = 8 * 1024 * 1024
    """Header-icon upload cap (default 8 MiB). Generous for SVG or
    high-DPI PNG wordmarks while still bounding accidental large uploads."""


class UploadsSettings(_Frozen):
    """App-wide caps for user-uploaded tool-input files.

    Tool authors declare a per-input ceiling on the descriptor
    (:attr:`matofx.tools.descriptor.InputField.max_file_size_mb`) and
    that's what the workspace enforces when present. When a tool
    leaves the field unset (``None``), the API falls back to
    :attr:`default_max_file_bytes` so a stray upload can't fill the
    shared mount before anyone notices.
    """

    default_max_file_bytes: Annotated[int, Field(ge=1, le=4 * 1024 * 1024 * 1024)] = (
        100 * 1024 * 1024
    )
    """Fallback per-file cap when a descriptor leaves ``max_file_size_mb`` unset.

    Default 100 MiB — comfortably above typical CSV / STEP / image
    uploads, well below "fills the mount in one click". Admins / super
    users tighten or relax this per-input via the Edit Tool page (the
    descriptor value wins when set)."""


class Settings(_Frozen):
    """Top-level MATOFX configuration.

    ``environment`` is required — there is intentionally no default. A
    silent default of ``"dev"`` on a customer install would be dangerous
    (debug-grade settings, lax timeouts, dev-only assertions). The
    operator must declare the environment explicitly in ``matofx.toml``
    or via ``MATOFX__ENVIRONMENT``.
    """

    environment: Environment
    log: LogSettings = Field(default_factory=LogSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)
    licence: LicenceSettings = Field(default_factory=LicenceSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    workflows: WorkflowsSettings = Field(default_factory=WorkflowsSettings)
    tutorials: TutorialsSettings = Field(default_factory=TutorialsSettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    coverage: CoverageSettings = Field(default_factory=CoverageSettings)
    uploads: UploadsSettings = Field(default_factory=UploadsSettings)
    branding: BrandingSettings = Field(default_factory=BrandingSettings)

    @model_validator(mode="after")
    def _production_safety_checks(self) -> Settings:
        """When ``environment == "prod"`` enforce / warn on dangerous combos.

        Hard-fail: missing audit signing key path, ``require_signing=False``.
        Soft-warn (via :mod:`warnings`): SQLite in prod, unusually long idle
        timeout, unusually high lockout threshold. Operators see the warnings
        on stderr; ``-W error`` promotes them to errors for stricter sites.
        """
        if self.environment != "prod":
            return self

        if self.audit.signing_key_path is None:
            raise ValueError(
                "audit.signing_key_path is not set, but environment='prod' "
                "requires the audit chain to be signed. Set "
                "audit.signing_key_path to your Ed25519 signing key path, "
                "or change environment to 'staging' / 'dev' / 'test' if this "
                "isn't a production install."
            )
        if self.licence.file is None:
            raise ValueError(
                "licence.file is not set, but environment='prod' requires a licence. "
                "Point [licence].file at the signed licence your supplier issued "
                "(see the admin guide, 'Your licence'), or change environment to "
                "'staging' / 'dev' / 'test' if this isn't a production install."
            )
        if not self.audit.require_signing:
            raise ValueError(
                "audit.require_signing is false, but environment='prod' "
                "requires it to be true. Set audit.require_signing = true "
                "(strongly recommended for production) or change environment."
            )

        if self.db.dialect == "sqlite":
            warnings.warn(
                "environment='prod' with db.dialect='sqlite'. Postgres is "
                "recommended for installs above ~5 concurrent users (see §B8 "
                "of the project brief).",
                stacklevel=2,
            )
        if self.auth.session_idle_timeout_minutes > _PROD_LONG_IDLE_TIMEOUT_MINUTES:
            warnings.warn(
                f"auth.session_idle_timeout_minutes={self.auth.session_idle_timeout_minutes} "
                "is unusually long for production (over 4 hours). Consider tightening.",
                stacklevel=2,
            )
        if self.auth.failed_login_lockout_threshold > _PROD_HIGH_LOCKOUT_THRESHOLD:
            warnings.warn(
                f"auth.failed_login_lockout_threshold={self.auth.failed_login_lockout_threshold} "
                "is unusually high for production. Most installs use 5-10.",
                stacklevel=2,
            )

        return self
