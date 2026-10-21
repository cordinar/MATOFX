"""Allow a combined scalar+field surrogate version (ADR-0018).

Revision ID: 0086
Revises: 0085
Create Date: 2026-06-30

Widens the ``surrogate_model_version.kind`` CHECK to admit ``combined`` — a
version holding N scalar surrogates **and** M field surrogates, predicted and
exported together (``designs/combined-surrogate.md``). SQLite can't widen a
CHECK in place, so the table is recreated via ``batch_alter_table`` (a no-op
rewrite on Postgres). No data migration — ``combined`` is only ever set by a
combined fit going forward; existing ``scalar``/``field`` rows are untouched.
"""

from __future__ import annotations

from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | None = None
depends_on: str | None = None

_OLD_KINDS = ("scalar", "field")
_NEW_KINDS = ("scalar", "field", "combined")


def _check(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(repr(k) for k in kinds) + ")"


def upgrade() -> None:
    """Widen the kind CHECK to admit ``combined``."""
    with op.batch_alter_table("surrogate_model_version", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(op.f("ck_surrogate_model_version_kind"), type_="check")
        batch.create_check_constraint(op.f("ck_surrogate_model_version_kind"), _check(_NEW_KINDS))


def downgrade() -> None:
    """Restore the narrower CHECK.

    ``combined`` versions can't be represented in the old vocabulary, so they
    are deleted first (downgrade is inherently destructive) — otherwise the
    batch table-rebuild's row-copy would violate the re-narrowed CHECK.
    """
    op.execute("DELETE FROM surrogate_model_version WHERE kind = 'combined'")
    with op.batch_alter_table("surrogate_model_version", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(op.f("ck_surrogate_model_version_kind"), type_="check")
        batch.create_check_constraint(op.f("ck_surrogate_model_version_kind"), _check(_OLD_KINDS))
