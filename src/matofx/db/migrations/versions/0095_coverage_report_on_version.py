"""Carry the design-space coverage verdict on a model version (ADR-0020 §9).

Revision ID: 0095
Revises: 0094
Create Date: 2026-08-24

Coverage is evidence about a model, not a panel someone looked at once: the
verdict, the criterion, the declared envelope and the per-factor breakdown
have to reach the model record, the per-job report and the V&V trail without
each of them recomputing it — and without the answer drifting between them.

Two columns rather than one. ``coverage_report_json`` is the whole structured
report; a flat scalar could not carry integer-lattice occupancy or the
per-response results, which is why §7 made the report structured in the first
place. ``coverage_verdict`` duplicates one field of it as a short indexed
string so a catalogue can filter or sort on "show me the models built from
thin data" without parsing every report on every row.

No new table. The report is a few kB, it is read whenever the version is read,
and it has no lifecycle of its own — it is written once at fit and never
amended, because a refit mints a *new* version rather than mutating this one.

**Both nullable, and that is load-bearing.** Every version fitted before this
feature existed has NULL here, and NULL means *"not assessed"* — never
"good". Silence about coverage and a clean bill of health are opposite
claims, and defaulting the column to a verdict would quietly convert one into
the other for every historical row.

The declared envelope lives *inside* the JSON and nowhere else, so the
predict-time guard (§8) and the V&V applicable range (§10) read the same
object the analysis produced rather than a second copy that can drift.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the report JSON + the indexed verdict to ``surrogate_model_version``."""
    with op.batch_alter_table("surrogate_model_version") as batch:
        batch.add_column(sa.Column("coverage_report_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("coverage_verdict", sa.String(length=16), nullable=True))
    op.create_index(
        "ix_surrogate_model_version_coverage_verdict",
        "surrogate_model_version",
        ["coverage_verdict"],
    )


def downgrade() -> None:
    """Drop both. Versions revert to carrying no coverage evidence at all."""
    op.drop_index(
        "ix_surrogate_model_version_coverage_verdict",
        table_name="surrogate_model_version",
    )
    with op.batch_alter_table("surrogate_model_version") as batch:
        batch.drop_column("coverage_verdict")
        batch.drop_column("coverage_report_json")
