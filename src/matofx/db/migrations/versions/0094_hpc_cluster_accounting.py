"""Carry a Slurm accounting identity on the cluster row (P-01 / D-01).

Revision ID: 0094
Revises: 0093
Create Date: 2026-08-13

Sites that charge cluster time to a project make every ``sbatch`` name
that project, and reject or mis-attribute submissions that don't. The
Slurm path emitted no such flag at all, so MATOFX could submit to those
clusters only by having the site turn its own accounting off.

Two columns, because Slurm has two separate identities and sites pick
one: ``account`` (``--account``, the common choice, tied to the
accounting database's association tree) and ``wckey`` (``--wckey``, a
free-form workload characterization key used where no association tree
exists). They are not aliases and a site may legitimately set both, so
neither is modelled as the other's fallback.

Both live on the *cluster* rather than the tool or the job: the identity
belongs to the relationship between this install and that cluster, and
an admin registering the cluster is the person who knows it. Both are
nullable, and ``NULL`` emits no flag — which is exactly the behaviour
every existing row had before this migration, so nothing changes for a
cluster that doesn't do accounting.

MS-HPC has no equivalent directive; the columns sit unused on an
``mshpc`` row rather than being split into a second table for two
strings.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``hpc_cluster.account`` and ``hpc_cluster.wckey``."""
    with op.batch_alter_table("hpc_cluster") as batch:
        batch.add_column(sa.Column("account", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("wckey", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Drop both columns. Submissions revert to emitting no accounting flag."""
    with op.batch_alter_table("hpc_cluster") as batch:
        batch.drop_column("wckey")
        batch.drop_column("account")
