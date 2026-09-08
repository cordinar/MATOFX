"""Tool versioning backbone — immutable snapshots + Job.tool_version_id capture.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-07

Adds:

- ``tool_version`` table — one immutable snapshot per descriptor /
  bundle change, with structured ``changes`` JSON for the change-log
  modal.
- ``job.tool_version_id`` — captures which snapshot a job was
  submitted against, so the runner stages the working dir from the
  exact bundle the operator clicked Submit with even if the tool's
  metadata is later edited or its bundle replaced.

Backfill is done in a single transaction:

1. For every existing ``tool`` row, insert a ``tool_version`` row
   that captures the current ``descriptor`` + ``bundle_path`` + the
   row's installer + a ``"Initial install (backfilled by migration
   0015)"`` summary.
2. For every existing ``job`` row, set ``job.tool_version_id`` to
   the snapshot id created in step 1 for the matching alias.
3. Add the NOT NULL constraint on ``job.tool_version_id`` (via batch
   alter so SQLite can recreate the table; Postgres tolerates a
   plain ``ALTER COLUMN ... SET NOT NULL``).

The downgrade drops the column + the table — no further rebalancing
of bundle paths because we never relocate existing on-disk bundles
in this migration (going forward, replace-bundle writes to per-version
subdirectories; existing bundles stay where they are).
"""

from __future__ import annotations

import datetime as _dt
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


_BACKFILL_SUMMARY = "Initial install (backfilled by migration 0015)"


def upgrade() -> None:
    """Create tool_version, add Job.tool_version_id, backfill, enforce NOT NULL."""
    op.create_table(
        "tool_version",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("descriptor_snapshot", sa.JSON(), nullable=False),
        sa.Column("bundle_path", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("change_summary", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["alias"],
            ["tool.alias"],
            ondelete="RESTRICT",
            name=op.f("fk_tool_version_alias_tool"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="RESTRICT",
            name=op.f("fk_tool_version_created_by_user_id_user"),
        ),
        sa.UniqueConstraint("alias", "version", name="uq_tool_version_alias_version"),
    )
    op.create_index("ix_tool_version_alias_created", "tool_version", ["alias", "created_at"])

    # Job.tool_version_id starts nullable so we can backfill, then we
    # tighten to NOT NULL once every existing job has been pointed at
    # the right snapshot.
    with op.batch_alter_table("job") as batch:
        batch.add_column(
            sa.Column(
                "tool_version_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            op.f("fk_job_tool_version_id_tool_version"),
            "tool_version",
            ["tool_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_job_tool_version_id", ["tool_version_id"])

    bind = op.get_bind()
    now = _dt.datetime.now(tz=_dt.UTC)

    # Step 1 — one tool_version row per existing tool.
    tool_rows = bind.execute(
        sa.text("SELECT alias, version, descriptor, bundle_path, installed_by_user_id FROM tool")
    ).fetchall()
    for row in tool_rows:
        alias, version, descriptor, bundle_path, installed_by_user_id = row
        if isinstance(descriptor, str):
            descriptor = json.loads(descriptor)
        bind.execute(
            sa.text(
                "INSERT INTO tool_version "
                "(alias, version, descriptor_snapshot, bundle_path, created_at, "
                " created_by_user_id, change_summary, changes) "
                "VALUES (:alias, :version, :snapshot, :bundle, :created_at, "
                "        :user_id, :summary, :changes)"
            ).bindparams(
                alias=alias,
                version=str(version),
                snapshot=json.dumps(descriptor),
                bundle=str(bundle_path),
                created_at=now,
                user_id=int(installed_by_user_id),
                summary=_BACKFILL_SUMMARY,
                changes=json.dumps({}),
            )
        )

    # Step 2 — point every existing job at its alias's seeded snapshot.
    bind.execute(
        sa.text(
            "UPDATE job SET tool_version_id = ("
            "  SELECT tv.id FROM tool_version tv WHERE tv.alias = job.tool_alias"
            ")"
        )
    )

    # Defence-in-depth: any job that didn't get a snapshot id at this
    # point would block the NOT NULL alter below — refuse loudly so the
    # operator sees the problem during migration rather than at
    # job-submit time.
    orphan_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM job WHERE tool_version_id IS NULL")
    ).scalar()
    if orphan_count and int(orphan_count) > 0:
        raise RuntimeError(
            f"Migration 0015 backfill: {orphan_count} job row(s) couldn't be "
            "matched to a tool_version snapshot. This usually means a "
            "tool_alias references a tool that no longer exists in the "
            "tool table. Investigate before re-running."
        )

    # Step 3 — NOT NULL on Job.tool_version_id.
    with op.batch_alter_table("job") as batch:
        batch.alter_column(
            "tool_version_id",
            existing_type=sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        )


def downgrade() -> None:
    """Drop Job.tool_version_id + tool_version. Bundles on disk untouched."""
    with op.batch_alter_table("job") as batch:
        batch.drop_index("ix_job_tool_version_id")
        batch.drop_constraint(op.f("fk_job_tool_version_id_tool_version"), type_="foreignkey")
        batch.drop_column("tool_version_id")

    op.drop_index("ix_tool_version_alias_created", table_name="tool_version")
    op.drop_table("tool_version")
