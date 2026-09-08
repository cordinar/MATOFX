"""ORM models for the execution-backend registries.

Five small lookup tables admins maintain through the UI:

- :class:`StorageLocation` — shared mount paths.
- :class:`PythonEnv` — registered Python interpreters.
- :class:`AosHost` — always-on server registry (SSH or WinRM).
- :class:`HpcCluster` — HPC head-node registry (Slurm or MS HPC).
- :class:`RuntimeSetting` — admin-tunable runtime knobs.

Each registry row is row-versioned for optimistic locking and is never
hard-deleted (set ``is_active=False`` and, for storage locations,
``retired_at``). Old jobs continue to resolve historical references.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class StorageLocation(_Base):
    """A shared mount path jobs can write their working directories under.

    Captured by id on every job at submit time so a future drive
    move (admin marks the old one ``retired_at``, points new jobs at
    a new row) doesn't break old jobs' result lookups.
    """

    __tablename__ = "storage_location"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    mount_path: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    """The same path on every host that mounts this share (e.g. ``\\\\fsxdrv1\\matofx``)."""

    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    retired_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    row_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012


class PythonEnv(_Base):
    """A registered Python interpreter tools may declare via descriptor.

    ``interpreter_path`` is either absolute or, when
    ``storage_location_id`` is set, relative to that location's
    ``mount_path``. Putting the env on a shared mount removes the
    need to install on every AOS / HPC compute node.
    """

    __tablename__ = "python_env"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    interpreter_path: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    storage_location_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("storage_location.id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    row_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012


class AosHost(_Base):
    """An always-on server MATOFX can reach over SSH (Linux) or WinRM (Windows).

    ``secret_ref`` is an opaque key into the secrets store — never
    the secret itself. The DB row is safe to back up and inspect.
    """

    __tablename__ = "aos_host"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    hostname: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    os: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """``"linux"`` or ``"windows"`` — pinned by CHECK constraint."""

    transport: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """``"ssh"`` (linux) or ``"winrm"`` (windows) — paired with ``os`` by CHECK."""

    port: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    secret_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    # --- capacity (the runner's per-host admission gate, ADR Part B) ---
    # An always-on server is a *dumb* persistent host — it has no scheduler
    # of its own (unlike HPC), so the runner gates how many jobs it sends.
    # A remote host can't be auto-detected, so capacity is admin-set.
    max_concurrency: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    """Most jobs the runner may run on this host at once. Always enforced."""

    cpu_cores: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Optional CPU-core budget. ``NULL`` ⇒ concurrency-cap only, no CPU budget."""

    memory_mb: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Optional memory budget (MB). ``NULL`` ⇒ concurrency-cap only, no RAM budget."""

    row_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint("os IN ('linux','windows')", name="ck_aos_host_os"),
        sa.CheckConstraint(
            "transport IN ('ssh','winrm')",
            name="ck_aos_host_transport",
        ),
        sa.CheckConstraint(
            "(os = 'linux' AND transport = 'ssh') OR (os = 'windows' AND transport = 'winrm')",
            name="ck_aos_host_os_transport",
        ),
        sa.CheckConstraint("port > 0 AND port <= 65535", name="ck_aos_host_port"),
        sa.CheckConstraint("max_concurrency >= 1", name="ck_aos_host_max_concurrency"),
        sa.CheckConstraint("cpu_cores IS NULL OR cpu_cores > 0", name="ck_aos_host_cpu_cores"),
        sa.CheckConstraint("memory_mb IS NULL OR memory_mb > 0", name="ck_aos_host_memory_mb"),
    )


class HpcCluster(_Base):
    """An HPC head-node entry — Slurm via SSH or MS HPC via WinRM."""

    __tablename__ = "hpc_cluster"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    scheduler: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """``"slurm"`` or ``"mshpc"`` — pinned by CHECK constraint."""

    head_node: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    transport: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    port: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    secret_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    default_queue: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    # --- Slurm accounting identity (migration 0094) ---
    # Sites that charge cluster time to a project reject or mis-attribute
    # submissions that don't name it. Slurm has two such identities and
    # sites pick one, so neither is the other's fallback. ``NULL`` emits
    # no flag. Both are ignored by the MS-HPC target, which has no
    # equivalent directive.
    account: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Slurm ``--account`` — the accounting-database association."""

    wckey: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Slurm ``--wckey`` — free-form workload characterization key."""

    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    row_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            "scheduler IN ('slurm','mshpc')",
            name="ck_hpc_cluster_scheduler",
        ),
        sa.CheckConstraint(
            "transport IN ('ssh','winrm')",
            name="ck_hpc_cluster_transport",
        ),
        sa.CheckConstraint(
            "(scheduler = 'slurm' AND transport = 'ssh') "
            "OR (scheduler = 'mshpc' AND transport = 'winrm')",
            name="ck_hpc_cluster_scheduler_transport",
        ),
        sa.CheckConstraint("port > 0 AND port <= 65535", name="ck_hpc_cluster_port"),
    )


RUNTIME_SETTING_DOMAINS: tuple[str, ...] = ("runner", "general")
"""Known catalog domains in the ``runtime_setting`` table.

Mirrors the CHECK constraint added by migration 0026. ``runner``
holds the daemon's hot-reloaded knobs (heartbeat intervals etc.);
``general`` holds the Admin → General Settings catalog (session,
notifications, housekeeping, branding). Each domain has its own
catalog (``matofx.runner.settings.RUNTIME_SETTINGS_CATALOG`` and
``matofx.config.general_settings.GENERAL_SETTINGS_CATALOG``)."""


class RuntimeSetting(_Base):
    """A single key/value runtime knob that admins can change at any time.

    Values are JSON-encoded so a single column holds ints, strings,
    and small structures uniformly. The runner daemon polls its
    domain every 30 s and hot-reloads its own behaviour, so changes
    take effect without a restart; the General Settings consumers
    pick up changes on-read.

    The composite ``(domain, key)`` primary key lets independent
    catalogs share the same column shape — e.g. both the runner and
    the general catalog could have a ``timeout_seconds`` key without
    colliding, though in practice keys are chosen to be globally
    unique for readability.
    """

    __tablename__ = "runtime_setting"

    domain: Mapped[str] = mapped_column(sa.String(16), primary_key=True, default="runner")
    """One of :data:`RUNTIME_SETTING_DOMAINS`. CHECK-constrained at the DB level."""

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(sa.Text, nullable=False)
    delegated_to_role: Mapped[str | None] = mapped_column(
        sa.String(32), nullable=True
    )
    """Per-row delegation grant: when set, callers of this role bypass the
    catalog's ``requires_role`` gate for value mutations only. Granting
    or clearing the delegation itself always remains a super-user
    action. ``None`` means no delegation in effect; only the
    catalog's ``requires_role`` applies."""

    updated_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
