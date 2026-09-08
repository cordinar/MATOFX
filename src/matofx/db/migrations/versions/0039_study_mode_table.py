"""Parametric studies: add the ``table`` sampling mode.

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-19

Backs the 2026-05-19 amendment to ADR-0001 / designs/parametric-studies.md
§2/§3/§4 (and decisions/0003-job-io-document-retention.md): a 10th
sampling mode ``table`` — a user-authored run matrix (the exact named
configurations the user enters), distinct from the cross-product
``list``. Only the ``ck_parametric_study_mode`` CHECK on
``parametric_study`` changes; the ``factor_spec`` / ``sampling_params``
shape additions for the new mode (and the per-factor grid
``levels``/``spacing``) are JSON-internal and need no DDL.

SQLite can't ALTER a CHECK in place, so the constraint is dropped and
recreated through a recreate-style ``batch_alter_table`` — the exact
technique migration 0031 used to widen ``ck_job_state`` (also 0010 /
0032 / 0038). Postgres takes the drop+create path through the same
batch API. The CHECK body is a **frozen literal** — migrations are
immutable historical records and must not import ``STUDY_MODES`` (a
later edit to that tuple must not retroactively change what this
revision does).

No data backfill: the v2 install is greenfield; every existing study
is in one of the original nine modes, all of which remain valid.
"""

from __future__ import annotations

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — do NOT replace with an import of STUDY_MODES.
_MODE_CHECK_WITH_TABLE = (
    "mode IN ('table','list','grid','frac_factorial','plackett_burman',"
    "'lhs','sobol','random','ccd','box_behnken')"
)
_MODE_CHECK_0032 = (
    "mode IN ('list','grid','frac_factorial','plackett_burman',"
    "'lhs','sobol','random','ccd','box_behnken')"
)


def upgrade() -> None:
    """Widen ck_parametric_study_mode to admit the ``table`` mode."""
    # Recreate-style batch so SQLite picks up the replacement CHECK
    # (it can't ALTER a CHECK on an existing table). Postgres takes
    # the drop+create path through the same batch API. Mirrors 0031.
    # ``op.f(...)`` marks the name as already-final so the metadata
    # naming convention (ck_%(table_name)s_%(constraint_name)s) is NOT
    # re-applied — 0032 created this CHECK with the same ``op.f`` idiom,
    # so the reflected name is the bare ``ck_parametric_study_mode``. A
    # plain string here would be re-conventionalised to
    # ``ck_parametric_study_ck_parametric_study_mode`` and the drop
    # would not match the reflected constraint.
    with op.batch_alter_table("parametric_study", recreate="always") as batch:
        batch.drop_constraint(op.f("ck_parametric_study_mode"), type_="check")
        batch.create_check_constraint(op.f("ck_parametric_study_mode"), _MODE_CHECK_WITH_TABLE)


def downgrade() -> None:
    """Restore the 0032 mode vocabulary.

    Any ``table`` study would violate the re-imposed CHECK, so those
    rows are deleted first; their child jobs and approval rows cascade
    via the ``ondelete=CASCADE`` FKs set in 0032 / 0038. This is an
    intentionally one-way lossy downgrade — a ``table`` run matrix has
    no meaningful coercion to another mode — in the same spirit as the
    one-way downgrades 0032 / 0038 use for their dual-home rows. A
    greenfield install has none of these rows.
    """
    op.execute("DELETE FROM parametric_study WHERE mode = 'table'")
    # ``op.f(...)`` — see the upgrade() note: keep the name final so the
    # convention is not doubled onto it.
    with op.batch_alter_table("parametric_study", recreate="always") as batch:
        batch.drop_constraint(op.f("ck_parametric_study_mode"), type_="check")
        batch.create_check_constraint(op.f("ck_parametric_study_mode"), _MODE_CHECK_0032)
