"""The install generates its own identity; Funis issues against it (ADR-0019 §A3).

Revision ID: 0092
Revises: 0091
Create Date: 2026-08-11

``install_id`` used to arrive **from Funis**, inside the signed licence or
identity file. That made it an identifier for the *licence*, not for the
install: feed the same licence to two deployments and both legitimately
claim the same id, so binding a licence to an install_id proved nothing.
A second instance could also simply never adopt an identity at all, and
run unbound indefinitely.

Inverting it — the install generates a random id at first boot, and Funis
issues a licence *for* that id — is the node-locked model every CAE
vendor uses, and it makes a second instance refuse to start: it generates
a different id, and the licence does not name it.

So the Funis-supplied columns become **nullable**: an install has an
identity from first boot, before any licence exists to describe who the
customer is or which report key they were given. Those fields are filled
in when a licence is installed.

Existing rows are untouched and keep working — their ``install_id`` was
Funis-issued, their licence names it, and the binding check passes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | None = None
depends_on: str | None = None

#: Columns that only a licence (or a legacy identity file) can supply.
_FUNIS_SUPPLIED = (
    ("customer", sa.String(255)),
    ("environment", sa.String(64)),
    ("issued_at", sa.DateTime(timezone=True)),
    ("report_key_id", sa.String(32)),
    ("issuer_key_id", sa.String(64)),
    ("identity_sha256", sa.String(64)),
)


def upgrade() -> None:
    """Allow an identity that exists before any Funis material does."""
    # batch_alter_table: SQLite cannot ALTER a column's nullability in
    # place, so Alembic rebuilds the table. Harmless on Postgres.
    with op.batch_alter_table("install_identity") as batch:
        for name, type_ in _FUNIS_SUPPLIED:
            batch.alter_column(name, existing_type=type_, nullable=True)
        batch.add_column(
            sa.Column(
                "self_generated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Restore NOT NULL. Fails if any self-generated identity exists.

    Deliberately not papered over with defaults: inventing a customer
    name to satisfy a constraint would put a fiction into the one table
    every compliance report is built from.
    """
    with op.batch_alter_table("install_identity") as batch:
        batch.drop_column("self_generated")
        for name, type_ in _FUNIS_SUPPLIED:
            batch.alter_column(name, existing_type=type_, nullable=False)
