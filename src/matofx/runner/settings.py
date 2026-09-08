"""Runtime settings the runner daemon hot-reloads from the database.

Five admin-tunable knobs live in the ``runtime_setting`` table
(seeded by migration 0012, ``domain='runner'``). Each value is
JSON-encoded so we can extend the catalog later without schema
changes.

The runner reads a fresh snapshot at startup and on a 30 s timer so
an administrator's change in the UI takes effect without a process
restart. Values that fail validation (out-of-range, wrong type)
fall back to the seeded default with an audit-relevant log line.

The :data:`RUNTIME_SETTINGS_CATALOG` is the single source of truth
for which keys exist, the value type each key expects, and the
valid range. Both the runner's loader (:func:`load_runtime_settings`,
permissive — clamps invalid values to defaults) and the admin
service (:class:`matofx.runner.admin_service.RuntimeSettingsService`,
strict — rejects invalid values with plain-English errors) consult
this same catalog.

The :class:`SettingSpec` / :data:`SettingKind` primitives are also
used by the General Settings catalog
(:mod:`matofx.config.general_settings`); they live here because the
runner catalog was the first consumer, not because they're runner-
specific. Either catalog can be looked up the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import select

from matofx.tools.exec_orm import RuntimeSetting
from matofx.users.roles import UserRole

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


SettingKind = Literal["int", "float", "str", "bool", "secret", "enum", "image"]
"""Value kinds the catalog can describe.

``int`` / ``float`` — numeric knobs with ``min_value`` / ``max_value``.
``str`` — free-form text (e.g. SMTP host, banner copy).
``bool`` — on/off toggle.
``secret`` — text that must never be returned by GET; UI renders as
  a masked input with a separate "Replace" action.
``enum`` — one of a fixed tuple of ``choices``.
``image`` — uploaded file (favicon, header icon). The DB stores just
  the basename; the actual bytes live under the configured branding
  directory. ``accepted_mime_types`` constrains uploads.
"""

SettingDefault = int | float | str | bool | None
"""Allowed types for a spec's ``default`` value.

Image kinds typically default to ``None`` (no override → bundled
default applies). Secrets typically default to ``None`` (unset).
"""


@dataclass(frozen=True)
class SettingSpec:
    """One entry in a settings catalog (runner or general).

    Carries the plain-English label and unit so the admin UI can
    render the setting without re-deriving anything; carries the
    inclusive ``min_value`` / ``max_value`` bounds (numeric kinds
    only) so the loader and the admin service share one validation
    rule.

    The first five fields are required for every kind. The rest
    are kind-specific and default to "not applicable":

    * Numeric kinds (``int`` / ``float``) must set ``min_value`` and
      ``max_value``. ``zero_means_never`` is meaningful only for
      ``int`` with ``min_value == 0``; when true, the loader and GC
      consumers treat ``0`` as "never apply this retention / never
      expire" rather than a regular numeric value.
    * Enum kinds must set ``choices`` and the ``default`` must be
      one of them.
    * Image kinds may set ``accepted_mime_types`` to constrain
      uploads (e.g. ``("image/png", "image/x-icon")``).
    * Secret kinds must set ``redacted=True``; GET responses replace
      the value with a sentinel so it never leaves the server.

    ``requires_role`` is the minimum :data:`~matofx.users.roles.UserRole`
    a caller needs to mutate the setting. The admin service checks
    this server-side; the UI uses it to lock rows the caller can't
    edit. Defaults to ``"administrator"`` so individual rows opt
    into ``"super_user"`` explicitly.

    ``delegatable_to`` declares that a super-user *may* grant a
    lower-tier role write access to this specific setting at
    runtime (per-row delegation). The DB column
    ``runtime_setting.delegated_to_role`` is the actual grant state:
    when set to a role string the spec's ``delegatable_to`` permits,
    callers with that role bypass the ``requires_role`` gate for
    value mutations only — they cannot grant / revoke the delegation
    itself (that always stays super-user). ``None`` (the default)
    means the setting can't be delegated.

    ``pending_restart`` flags settings the application reads once at
    startup (rare — most settings hot-reload). The UI shows a
    "Restart required" chip after save so operators aren't surprised.
    """

    key: str
    kind: SettingKind
    default: SettingDefault
    label: str
    description: str

    # Numeric kinds
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    zero_means_never: bool = False

    # Enum kind
    choices: tuple[str, ...] | None = None

    # Secret kind
    redacted: bool = False

    # Image kind
    accepted_mime_types: tuple[str, ...] | None = None

    # Access + behaviour (apply to every kind)
    requires_role: UserRole = "administrator"
    delegatable_to: UserRole | None = None
    pending_restart: bool = False

    def __post_init__(self) -> None:
        """Reject internally inconsistent specs at module-load time.

        Catalog typos (e.g. an enum entry whose default isn't in its
        choices) become an import-time error instead of a baffling
        runtime failure when an admin first edits the row.
        """
        if self.kind in ("int", "float"):
            if self.min_value is None or self.max_value is None:
                raise ValueError(
                    f"{self.key}: kind={self.kind!r} requires min_value and max_value."
                )
            if self.min_value > self.max_value:
                raise ValueError(
                    f"{self.key}: min_value ({self.min_value}) > max_value ({self.max_value})."
                )
        if self.kind == "bool" and not isinstance(self.default, bool):
            raise ValueError(
                f"{self.key}: kind='bool' requires a bool default "
                f"(got {type(self.default).__name__})."
            )
        if self.kind == "enum":
            if not self.choices:
                raise ValueError(f"{self.key}: kind='enum' requires non-empty choices.")
            if self.default not in self.choices:
                raise ValueError(
                    f"{self.key}: default {self.default!r} not in choices {self.choices!r}."
                )
        if self.kind == "secret" and not self.redacted:
            raise ValueError(f"{self.key}: kind='secret' requires redacted=True.")
        if self.kind == "image" and self.default is not None and not isinstance(self.default, str):
            raise ValueError(
                f"{self.key}: kind='image' default must be a filename string or None "
                f"(got {type(self.default).__name__})."
            )
        if self.zero_means_never:
            if self.kind != "int":
                raise ValueError(
                    f"{self.key}: zero_means_never only applies to kind='int' (got {self.kind!r})."
                )
            if self.min_value != 0:
                raise ValueError(
                    f"{self.key}: zero_means_never requires min_value=0 (got {self.min_value})."
                )
        # Delegation only makes sense when there's somewhere lower
        # (i.e. less privileged) to delegate *to*. ``_ROLE_RANK`` uses
        # smaller numbers for higher privilege (super_user=0 is the
        # most privileged), so a valid delegation has
        # ``rank[delegatable_to] > rank[requires_role]``. Catalog typos
        # that point delegation at a peer or higher-privileged role
        # surface as import-time errors rather than runtime surprises.
        if self.delegatable_to is not None:
            from matofx.users.roles import _ROLE_RANK  # noqa: PLC0415

            if self.delegatable_to not in _ROLE_RANK:
                raise ValueError(
                    f"{self.key}: delegatable_to={self.delegatable_to!r} "
                    f"isn't a known role."
                )
            if _ROLE_RANK[self.delegatable_to] <= _ROLE_RANK[self.requires_role]:
                raise ValueError(
                    f"{self.key}: delegatable_to ({self.delegatable_to!r}) must be "
                    f"less privileged than requires_role ({self.requires_role!r})."
                )


RUNTIME_SETTINGS_CATALOG: Final[dict[str, SettingSpec]] = {
    "local_concurrency": SettingSpec(
        key="local_concurrency",
        kind="int",
        min_value=1,
        max_value=1024,
        default=2,
        label="Local concurrency",
        unit="jobs",
        description=(
            "How many jobs the runner runs at once on the local host. "
            "Higher values use more CPU and memory."
        ),
    ),
    "heartbeat_local_aos_interval_s": SettingSpec(
        key="heartbeat_local_aos_interval_s",
        kind="float",
        min_value=0.1,
        max_value=60.0,
        default=2.0,
        label="Heartbeat poll interval (local / always-on)",
        unit="seconds",
        description=(
            "How often the runner re-reads the heartbeat file for jobs running "
            "locally or on always-on servers. Smaller values surface state changes faster."
        ),
    ),
    "heartbeat_hpc_running_interval_s": SettingSpec(
        key="heartbeat_hpc_running_interval_s",
        kind="float",
        min_value=1.0,
        max_value=600.0,
        default=10.0,
        label="Heartbeat poll interval (HPC, running)",
        unit="seconds",
        description="How often the runner re-reads the heartbeat for HPC jobs known to be running.",
    ),
    "heartbeat_hpc_queued_interval_s": SettingSpec(
        key="heartbeat_hpc_queued_interval_s",
        kind="float",
        min_value=1.0,
        max_value=3600.0,
        default=30.0,
        label="Heartbeat poll interval (HPC, queued)",
        unit="seconds",
        description=(
            "How often the runner asks the HPC scheduler about jobs that haven't started yet. "
            "Most clusters dislike sub-second polling here."
        ),
    ),
    "cancel_grace_s": SettingSpec(
        key="cancel_grace_s",
        kind="int",
        min_value=0,
        max_value=600,
        default=30,
        label="Cancel grace period",
        unit="seconds",
        description=(
            "After a cancel is requested, the runner sends a polite stop signal and "
            "waits this long before forcing the job to terminate."
        ),
    ),
    "gpu_concurrency": SettingSpec(
        key="gpu_concurrency",
        kind="int",
        min_value=1,
        max_value=64,
        default=1,
        label="Local GPU concurrency",
        unit="jobs",
        description=(
            "How many GPU jobs the runner runs at once on the local host. "
            "Defaults to 1 so two GPU jobs can't exhaust device memory at the "
            "same time. A local GPU job also consumes a local-concurrency slot."
        ),
    ),
    "local_cpu_cores": SettingSpec(
        key="local_cpu_cores",
        kind="int",
        min_value=0,
        max_value=4096,
        default=0,
        label="Local CPU budget",
        unit="cores",
        description=(
            "Total CPU cores the runner may hand out to local jobs at once. The "
            "runner starts a local job only when the unused budget covers the "
            "cores its descriptor declares — a free concurrency slot alone isn't "
            "enough. 0 means auto-detect the host's logical cores."
        ),
    ),
    "local_memory_mb": SettingSpec(
        key="local_memory_mb",
        kind="int",
        min_value=0,
        max_value=8_388_608,  # 8 TiB ceiling — generous headroom for large hosts
        default=0,
        label="Local memory budget",
        unit="MB",
        description=(
            "Total memory the runner may hand out to local jobs at once. The "
            "runner starts a local job only when the unused budget covers the "
            "memory its descriptor declares. 0 means auto-detect the host's "
            "total RAM minus the memory reserve below."
        ),
    ),
    "local_memory_reserve_mb": SettingSpec(
        key="local_memory_reserve_mb",
        kind="int",
        min_value=0,
        max_value=1_048_576,  # 1 TiB
        default=2048,
        label="Local memory reserve",
        unit="MB",
        description=(
            "Memory held back for the operating system and MATOFX itself when the "
            "local memory budget is auto-detected. Ignored when an explicit local "
            "memory budget is set."
        ),
    ),
    "study_concurrency_cap": SettingSpec(
        key="study_concurrency_cap",
        kind="int",
        min_value=1,
        max_value=1024,
        default=10,
        label="Parametric-study concurrency cap",
        unit="jobs per study",
        description=(
            "The most child jobs from a single parametric study the runner "
            "will run at once. Stops a large sweep from seizing every worker "
            "and starving interactive jobs; study children also queue behind "
            "ordinary and workflow jobs of equal priority."
        ),
    ),
    "local_job_walltime_minutes": SettingSpec(
        key="local_job_walltime_minutes",
        kind="int",
        min_value=0,
        max_value=10080,
        default=0,
        zero_means_never=True,
        label="Local job walltime limit",
        unit="minutes",
        description=(
            "Maximum wall-clock time a job may run on the local host before the "
            "runner stops it and marks it failed. Guards against a runaway tool "
            "(e.g. a solver that won't converge) consuming a worker forever. "
            "0 means no limit. A tool's own descriptor walltime, when set, takes "
            "precedence over this default."
        ),
    ),
}
"""Closed catalog of admin-tunable runtime knobs. Adding a new
setting is a code change in two places: this catalog *and* the
:class:`RuntimeSettings` field. Migrations seed the default."""


@dataclass(frozen=True)
class RuntimeSettings:
    """Snapshot of the admin-tunable runner knobs.

    Frozen so a misbehaving consumer can't mutate the snapshot
    another consumer is still reading. Hot-reload publishes a new
    instance and atomically swaps the reference; readers always
    see a coherent set.
    """

    local_concurrency: int
    """Max concurrent jobs on the local target. 1-1024 inclusive."""

    heartbeat_local_aos_interval_s: float
    """Seconds between heartbeat-file reads for local + AOS jobs."""

    heartbeat_hpc_running_interval_s: float
    """Seconds between heartbeat reads for HPC jobs known to be running."""

    heartbeat_hpc_queued_interval_s: float
    """Seconds between heartbeat reads for HPC jobs still queued."""

    cancel_grace_s: int
    """Wait this long between SIGTERM and SIGKILL on cancel."""

    study_concurrency_cap: int
    """Max concurrent child jobs per parametric study. 1-1024 inclusive."""

    local_job_walltime_minutes: int = 0
    """Kill + fail a local job once it exceeds this wall-clock time.
    0 = no limit (a tool's descriptor walltime, if set, overrides). Defaulted
    so existing call sites that construct the snapshot directly keep working."""

    gpu_concurrency: int = 1
    """Max concurrent *local GPU* jobs (``requires_gpu`` + local target).
    Default 1 — strict one-at-a-time so GPU jobs don't fight over device
    memory. A GPU job also consumes a ``local_concurrency`` slot. Defaulted
    so existing call sites that construct the snapshot directly keep working."""

    local_cpu_cores: int = 0
    """CPU-core budget the runner hands out to local jobs (0 = auto-detect
    the host). A local job starts only when the unused budget covers its
    declared cores."""

    local_memory_mb: int = 0
    """Memory budget (MB) the runner hands out to local jobs (0 = auto-detect
    the host total minus :attr:`local_memory_reserve_mb`)."""

    local_memory_reserve_mb: int = 2048
    """RAM held back for the OS + MATOFX when the memory budget is
    auto-detected; ignored when an explicit budget is set."""


def _int_default(key: str) -> int:
    """Return ``RUNTIME_SETTINGS_CATALOG[key].default`` as ``int``.

    Catalog ``default`` is :data:`SettingDefault` (``int | float |
    str | bool | None``) to support all kinds; the runner catalog
    only carries numeric entries, so a runtime assertion documents
    and enforces that for the static type checker.
    """
    default = RUNTIME_SETTINGS_CATALOG[key].default
    assert isinstance(default, int | float) and not isinstance(default, bool)
    return int(default)


def _float_default(key: str) -> float:
    default = RUNTIME_SETTINGS_CATALOG[key].default
    assert isinstance(default, int | float) and not isinstance(default, bool)
    return float(default)


DEFAULT_RUNTIME_SETTINGS: RuntimeSettings = RuntimeSettings(
    local_concurrency=_int_default("local_concurrency"),
    heartbeat_local_aos_interval_s=_float_default("heartbeat_local_aos_interval_s"),
    heartbeat_hpc_running_interval_s=_float_default("heartbeat_hpc_running_interval_s"),
    heartbeat_hpc_queued_interval_s=_float_default("heartbeat_hpc_queued_interval_s"),
    cancel_grace_s=_int_default("cancel_grace_s"),
    study_concurrency_cap=_int_default("study_concurrency_cap"),
    local_job_walltime_minutes=_int_default("local_job_walltime_minutes"),
    gpu_concurrency=_int_default("gpu_concurrency"),
    local_cpu_cores=_int_default("local_cpu_cores"),
    local_memory_mb=_int_default("local_memory_mb"),
    local_memory_reserve_mb=_int_default("local_memory_reserve_mb"),
)
"""Hard-coded fallbacks matching the seed in migration 0012.

Used when the table is unreachable, when a row is missing entirely,
or when a value fails validation. Keeps the runner up even if the
DB is briefly unavailable mid-flight."""


_VALID_RANGES: dict[str, tuple[float, float]] = {
    k: (s.min_value, s.max_value)
    for k, s in RUNTIME_SETTINGS_CATALOG.items()
    if s.min_value is not None and s.max_value is not None
}


async def load_runtime_settings(session: AsyncSession) -> RuntimeSettings:
    """Read every ``runtime_setting`` row and assemble a :class:`RuntimeSettings`.

    Missing keys → the corresponding field on
    :data:`DEFAULT_RUNTIME_SETTINGS`. Out-of-range or wrong-typed
    values → same fallback. The function never raises on bad data;
    the worst case is "we run with the defaults", which keeps the
    runner alive across operator typos.
    """
    rows = (
        (await session.execute(select(RuntimeSetting).where(RuntimeSetting.domain == "runner")))
        .scalars()
        .all()
    )
    raw: dict[str, object] = {}
    for row in rows:
        try:
            raw[row.key] = json.loads(row.value_json)
        except (TypeError, ValueError):
            continue

    return RuntimeSettings(
        local_concurrency=_coerce_int(
            raw.get("local_concurrency"),
            "local_concurrency",
            DEFAULT_RUNTIME_SETTINGS.local_concurrency,
        ),
        heartbeat_local_aos_interval_s=_coerce_float(
            raw.get("heartbeat_local_aos_interval_s"),
            "heartbeat_local_aos_interval_s",
            DEFAULT_RUNTIME_SETTINGS.heartbeat_local_aos_interval_s,
        ),
        heartbeat_hpc_running_interval_s=_coerce_float(
            raw.get("heartbeat_hpc_running_interval_s"),
            "heartbeat_hpc_running_interval_s",
            DEFAULT_RUNTIME_SETTINGS.heartbeat_hpc_running_interval_s,
        ),
        heartbeat_hpc_queued_interval_s=_coerce_float(
            raw.get("heartbeat_hpc_queued_interval_s"),
            "heartbeat_hpc_queued_interval_s",
            DEFAULT_RUNTIME_SETTINGS.heartbeat_hpc_queued_interval_s,
        ),
        cancel_grace_s=_coerce_int(
            raw.get("cancel_grace_s"),
            "cancel_grace_s",
            DEFAULT_RUNTIME_SETTINGS.cancel_grace_s,
        ),
        study_concurrency_cap=_coerce_int(
            raw.get("study_concurrency_cap"),
            "study_concurrency_cap",
            DEFAULT_RUNTIME_SETTINGS.study_concurrency_cap,
        ),
        local_job_walltime_minutes=_coerce_int(
            raw.get("local_job_walltime_minutes"),
            "local_job_walltime_minutes",
            DEFAULT_RUNTIME_SETTINGS.local_job_walltime_minutes,
        ),
        gpu_concurrency=_coerce_int(
            raw.get("gpu_concurrency"),
            "gpu_concurrency",
            DEFAULT_RUNTIME_SETTINGS.gpu_concurrency,
        ),
        local_cpu_cores=_coerce_int(
            raw.get("local_cpu_cores"),
            "local_cpu_cores",
            DEFAULT_RUNTIME_SETTINGS.local_cpu_cores,
        ),
        local_memory_mb=_coerce_int(
            raw.get("local_memory_mb"),
            "local_memory_mb",
            DEFAULT_RUNTIME_SETTINGS.local_memory_mb,
        ),
        local_memory_reserve_mb=_coerce_int(
            raw.get("local_memory_reserve_mb"),
            "local_memory_reserve_mb",
            DEFAULT_RUNTIME_SETTINGS.local_memory_reserve_mb,
        ),
    )


def _coerce_int(value: object, key: str, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return _clamp_int(value, key, default)


def _coerce_float(value: object, key: str, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return _clamp_float(float(value), key, default)


def _clamp_int(value: int, key: str, default: int) -> int:
    bounds = _VALID_RANGES.get(key)
    if bounds is None:
        return value
    low, high = bounds
    if value < low or value > high:
        return default
    return value


def _clamp_float(value: float, key: str, default: float) -> float:
    bounds = _VALID_RANGES.get(key)
    if bounds is None:
        return value
    low, high = bounds
    if value < low or value > high:
        return default
    return value
