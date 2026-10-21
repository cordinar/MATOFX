"""Execution backend schema: storage, env, AOS/HPC targets, runtime settings.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-04

Lays the groundwork for the ``matofx.tools`` execution backend.

New tables:

- ``storage_location`` — registry of shared mount paths jobs can use.
  Recorded on every job at submit time; rows are never deleted, only
  ``retired_at``-stamped, so old jobs keep resolving when admins move
  to a newer share.
- ``python_env`` — registry of Python interpreters tools may run
  under. Either an absolute path or a path relative to a
  ``storage_location`` (so a single shared-FS env can be referenced
  from local, AOS, and HPC targets with no per-node install).
- ``aos_host`` — always-on server registry (Linux+SSH or
  Windows+WinRM). Credentials are referenced by opaque ``secret_ref``;
  the actual key lives in the secrets store.
- ``hpc_cluster`` — HPC head-node registry (Slurm via SSH, MS HPC
  via WinRM). Same secret_ref pattern.
- ``runtime_setting`` — admin-tunable runtime knobs (local
  concurrency cap, heartbeat poll intervals, cancel grace period).
  Hot-reloaded by the runner daemon every 30 s so admins don't have
  to restart anything.

Tool gets execution-target columns (``execution_target``,
``aos_host_id``, ``hpc_cluster_id``, ``python_env_id``,
``resource_request``, ``requires_gpu``) plus CHECK constraints
keeping the FKs consistent with the chosen target.

Job gets execution-tracking columns (``storage_location_id``,
``working_dir_relative``, ``scheduler_job_id``, ``heartbeat_state``,
``heartbeat_last_seen_at``, ``exit_code``, ``cancel_requested_at``,
``cancel_requested_by_user_id``).

Six new permission slugs are seeded into the ``administrator`` role
(except ``jobs.cancel_any``, which is super-user-only — admins cancel
via the project-access ladder, not a global override).
"""

from __future__ import annotations

import datetime as _dt
import json

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


# Permission slugs added in this migration. The administrator role gets
# everything except ``jobs.cancel_any`` (super-user-only sentinel).
_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = (
    "runtime_settings.manage",
    "aos_hosts.manage",
    "hpc_clusters.manage",
    "storage_locations.manage",
    "python_envs.manage",
)
_NEW_SUPER_USER_ONLY_PERMISSIONS: tuple[str, ...] = ("jobs.cancel_any",)


# Default runtime settings seeded into the runtime_setting table.
_DEFAULT_RUNTIME_SETTINGS: tuple[tuple[str, object], ...] = (
    ("local_concurrency", 2),
    ("heartbeat_local_aos_interval_s", 2),
    ("heartbeat_hpc_running_interval_s", 10),
    ("heartbeat_hpc_queued_interval_s", 30),
    ("cancel_grace_s", 30),
)


def upgrade() -> None:
    """Create execution-backend tables, extend tool/job, seed defaults."""
    _create_storage_location()
    _create_python_env()
    _create_aos_host()
    _create_hpc_cluster()
    _create_runtime_setting()
    _extend_tool()
    _extend_job()
    _seed_runtime_settings()
    _seed_admin_permissions()


def downgrade() -> None:
    """Reverse — drop seeded data and tables in dependency-safe order."""
    _unseed_admin_permissions()
    _unseed_runtime_settings()
    _shrink_job()
    _shrink_tool()
    op.drop_table("runtime_setting")
    op.drop_table("hpc_cluster")
    op.drop_table("aos_host")
    op.drop_table("python_env")
    op.drop_table("storage_location")


# ---------------------------------------------------------------------------
# Table creation helpers
# ---------------------------------------------------------------------------


def _create_storage_location() -> None:
    op.create_table(
        "storage_location",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("mount_path", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_storage_location")),
        sa.UniqueConstraint("name", name=op.f("uq_storage_location_name")),
    )


def _create_python_env() -> None:
    op.create_table(
        "python_env",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("interpreter_path", sa.String(length=512), nullable=False),
        sa.Column(
            "storage_location_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_python_env")),
        sa.UniqueConstraint("name", name=op.f("uq_python_env_name")),
        sa.ForeignKeyConstraint(
            ["storage_location_id"],
            ["storage_location.id"],
            name=op.f("fk_python_env_storage_location_id_storage_location"),
            ondelete="RESTRICT",
        ),
    )


def _create_aos_host() -> None:
    op.create_table(
        "aos_host",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("os", sa.String(length=16), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_aos_host")),
        sa.UniqueConstraint("name", name=op.f("uq_aos_host_name")),
        sa.CheckConstraint("os IN ('linux','windows')", name=op.f("ck_aos_host_os")),
        sa.CheckConstraint(
            "transport IN ('ssh','winrm')",
            name=op.f("ck_aos_host_transport"),
        ),
        sa.CheckConstraint(
            "(os = 'linux' AND transport = 'ssh') OR (os = 'windows' AND transport = 'winrm')",
            name=op.f("ck_aos_host_os_transport"),
        ),
        sa.CheckConstraint("port > 0 AND port <= 65535", name=op.f("ck_aos_host_port")),
    )


def _create_hpc_cluster() -> None:
    op.create_table(
        "hpc_cluster",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("scheduler", sa.String(length=16), nullable=False),
        sa.Column("head_node", sa.String(length=255), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=255), nullable=False),
        sa.Column("default_queue", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hpc_cluster")),
        sa.UniqueConstraint("name", name=op.f("uq_hpc_cluster_name")),
        sa.CheckConstraint(
            "scheduler IN ('slurm','mshpc')",
            name=op.f("ck_hpc_cluster_scheduler"),
        ),
        sa.CheckConstraint(
            "transport IN ('ssh','winrm')",
            name=op.f("ck_hpc_cluster_transport"),
        ),
        sa.CheckConstraint(
            "(scheduler = 'slurm' AND transport = 'ssh') "
            "OR (scheduler = 'mshpc' AND transport = 'winrm')",
            name=op.f("ck_hpc_cluster_scheduler_transport"),
        ),
        sa.CheckConstraint("port > 0 AND port <= 65535", name=op.f("ck_hpc_cluster_port")),
    )


def _create_runtime_setting() -> None:
    op.create_table(
        "runtime_setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column(
            "updated_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_runtime_setting")),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user.id"],
            name=op.f("fk_runtime_setting_updated_by_user_id_user"),
            ondelete="SET NULL",
        ),
    )


# ---------------------------------------------------------------------------
# Existing-table extensions
# ---------------------------------------------------------------------------


def _extend_tool() -> None:
    """Add execution-target columns + cross-target CHECK constraints to ``tool``.

    Rebuilds the table on SQLite (``recreate_on_sqlite``) so it picks up the new
    multi-column CHECK constraints (it can't add them to an existing
    table otherwise). Postgres adds them with ``ALTER TABLE``.
    """
    with op.batch_alter_table("tool", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(
            sa.Column(
                "execution_target",
                sa.String(length=16),
                nullable=False,
                server_default="local",
            )
        )
        batch.add_column(
            sa.Column(
                "aos_host_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "hpc_cluster_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "python_env_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("resource_request", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column(
                "requires_gpu",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.create_foreign_key(
            op.f("fk_tool_aos_host_id_aos_host"),
            "aos_host",
            ["aos_host_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_tool_hpc_cluster_id_hpc_cluster"),
            "hpc_cluster",
            ["hpc_cluster_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_tool_python_env_id_python_env"),
            "python_env",
            ["python_env_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "execution_target",
            "execution_target IN ('local','aos','hpc')",
        )
        batch.create_check_constraint(
            "execution_target_local",
            "execution_target <> 'local' OR (aos_host_id IS NULL AND hpc_cluster_id IS NULL)",
        )
        batch.create_check_constraint(
            "execution_target_aos",
            "execution_target <> 'aos' OR (aos_host_id IS NOT NULL AND hpc_cluster_id IS NULL)",
        )
        batch.create_check_constraint(
            "execution_target_hpc",
            "execution_target <> 'hpc' OR (hpc_cluster_id IS NOT NULL AND aos_host_id IS NULL)",
        )


def _extend_job() -> None:
    """Add execution-tracking columns to ``job`` (all nullable, all set at runtime)."""
    with op.batch_alter_table("job", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(
            sa.Column(
                "storage_location_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("working_dir_relative", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column("scheduler_job_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("heartbeat_state", sa.String(length=16), nullable=True))
        batch.add_column(
            sa.Column("heartbeat_last_seen_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("exit_code", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "cancel_requested_by_user_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            op.f("fk_job_storage_location_id_storage_location"),
            "storage_location",
            ["storage_location_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_job_cancel_requested_by_user_id_user"),
            "user",
            ["cancel_requested_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "heartbeat_state",
            "heartbeat_state IS NULL "
            "OR heartbeat_state IN ('pending','running','complete','failed')",
        )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_runtime_settings() -> None:
    now = _dt.datetime.now(tz=_dt.UTC)
    table = sa.table(
        "runtime_setting",
        sa.column("key", sa.String()),
        sa.column("value_json", sa.Text()),
        sa.column("updated_by_user_id", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        table,
        [
            {
                "key": key,
                "value_json": json.dumps(value),
                "updated_by_user_id": None,
                "updated_at": now,
            }
            for key, value in _DEFAULT_RUNTIME_SETTINGS
        ],
    )


def _seed_admin_permissions() -> None:
    """Grant the admin-managed permissions to the ``administrator`` built-in role.

    ``jobs.cancel_any`` is intentionally NOT seeded — it's the
    sentinel-managed super-user override; admins cancel via the
    project-access ladder, not a global flag.
    """
    bind = op.get_bind()
    admin_id_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n="administrator")
    ).fetchone()
    if admin_id_row is None:
        return  # Greenfield install with no roles seeded; nothing to do.
    admin_id: int = admin_id_row[0]

    now = _dt.datetime.now(tz=_dt.UTC)
    table = sa.table(
        "role_permission",
        sa.column("role_id", sa.Integer()),
        sa.column("permission", sa.String()),
        sa.column("granted_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        table,
        [
            {"role_id": admin_id, "permission": perm, "granted_at": now}
            for perm in _NEW_ADMIN_PERMISSIONS
        ],
    )


def _unseed_admin_permissions() -> None:
    bind = op.get_bind()
    all_new = (*_NEW_ADMIN_PERMISSIONS, *_NEW_SUPER_USER_ONLY_PERMISSIONS)
    for perm in all_new:
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )


def _unseed_runtime_settings() -> None:
    bind = op.get_bind()
    for key, _ in _DEFAULT_RUNTIME_SETTINGS:
        bind.execute(sa.text("DELETE FROM runtime_setting WHERE key = :k").bindparams(k=key))


# ---------------------------------------------------------------------------
# Downgrade column drops
# ---------------------------------------------------------------------------


def _shrink_tool() -> None:
    # ``drop_constraint`` re-applies the naming convention to the supplied name
    # under ``batch_alter_table``, so the bare user-supplied label (the same
    # one given to ``create_check_constraint`` / ``op.f`` on the way up) is
    # what we pass here, not the convention-expanded full name.
    with op.batch_alter_table("tool", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("execution_target_hpc", type_="check")
        batch.drop_constraint("execution_target_aos", type_="check")
        batch.drop_constraint("execution_target_local", type_="check")
        batch.drop_constraint("execution_target", type_="check")
        batch.drop_constraint(
            op.f("fk_tool_python_env_id_python_env"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_tool_hpc_cluster_id_hpc_cluster"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_tool_aos_host_id_aos_host"),
            type_="foreignkey",
        )
        batch.drop_column("requires_gpu")
        batch.drop_column("resource_request")
        batch.drop_column("python_env_id")
        batch.drop_column("hpc_cluster_id")
        batch.drop_column("aos_host_id")
        batch.drop_column("execution_target")


def _shrink_job() -> None:
    with op.batch_alter_table("job", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("heartbeat_state", type_="check")
        batch.drop_constraint(
            op.f("fk_job_cancel_requested_by_user_id_user"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_job_storage_location_id_storage_location"),
            type_="foreignkey",
        )
        batch.drop_column("cancel_requested_by_user_id")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("exit_code")
        batch.drop_column("heartbeat_last_seen_at")
        batch.drop_column("heartbeat_state")
        batch.drop_column("scheduler_job_id")
        batch.drop_column("working_dir_relative")
        batch.drop_column("storage_location_id")
