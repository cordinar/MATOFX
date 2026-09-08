"""AOS host capacity columns — the runner's per-host admission gate.

Revision ID: 0078
Revises: 0077
Create Date: 2026-06-12

Part B of the execution-scheduling work (``designs/execution-scheduling.md``).
An always-on server is a *dumb* persistent host with no scheduler of its
own (unlike HPC, where the cluster owns its queue), so the runner must gate
how many jobs it dispatches there. A remote host can't be auto-detected the
way the local host is, so capacity is admin-set per host:

- ``max_concurrency`` (NOT NULL, default 1) — always enforced job-count cap.
- ``cpu_cores`` (nullable) — optional CPU-core budget; ``NULL`` = count-cap only.
- ``memory_mb`` (nullable) — optional memory budget; ``NULL`` = count-cap only.

SQLite can't add multi-column-free CHECK constraints in place, so the
table is recreated via ``batch_alter_table`` (a no-op rewrite on Postgres).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the three capacity columns + their CHECK constraints to ``aos_host``."""
    with op.batch_alter_table("aos_host", recreate="always") as batch:
        batch.add_column(
            sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("cpu_cores", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("memory_mb", sa.Integer(), nullable=True))
        batch.create_check_constraint("aos_host_max_concurrency", "max_concurrency >= 1")
        batch.create_check_constraint(
            "aos_host_cpu_cores", "cpu_cores IS NULL OR cpu_cores > 0"
        )
        batch.create_check_constraint(
            "aos_host_memory_mb", "memory_mb IS NULL OR memory_mb > 0"
        )


def downgrade() -> None:
    """Drop the capacity columns + constraints."""
    with op.batch_alter_table("aos_host", recreate="always") as batch:
        batch.drop_constraint("aos_host_memory_mb", type_="check")
        batch.drop_constraint("aos_host_cpu_cores", type_="check")
        batch.drop_constraint("aos_host_max_concurrency", type_="check")
        batch.drop_column("memory_mb")
        batch.drop_column("cpu_cores")
        batch.drop_column("max_concurrency")
