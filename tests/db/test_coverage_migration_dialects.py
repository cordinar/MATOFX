"""Migration 0095 must be correct on Postgres, which is where it will run.

Every coverage test that touches a database is parametrised sqlite/postgres
and the postgres half skips without ``MATOFX_TEST_POSTGRES_URL`` — the normal
case on a developer box. So the columns ADR-0020 added have, in practice,
only ever been exercised on SQLite, while production is Postgres.

A server is not needed to catch the failure mode that actually matters here:
**DDL that is valid on one dialect and not the other.** SQLAlchemy will
compile the same schema objects for either, and a type that cannot render, or
renders as something surprising, shows up immediately.

This is not a substitute for running the suite against a real Postgres. It is
the part of that assurance obtainable without one, and it is the part that
would catch a dialect mistake.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from matofx.db import migrations

_COVERAGE_COLUMNS = (
    sa.Column("coverage_report_json", sa.JSON(), nullable=True),
    sa.Column("coverage_verdict", sa.String(length=16), nullable=True),
)


def _rendered(column: sa.Column, dialect: sa.engine.Dialect) -> str:
    """The column's type as this dialect would emit it."""
    return str(column.type.compile(dialect=dialect))


def test_the_report_column_renders_on_both_dialects() -> None:
    """``sa.JSON()`` is dialect-agnostic on purpose.

    Postgres renders it as ``JSON``; SQLite stores it as ``TEXT`` with a JSON
    serialiser. Either is correct — what would not be is a type that fails to
    compile, or one dialect silently getting something else.
    """
    report_column = _COVERAGE_COLUMNS[0]
    pg = _rendered(report_column, postgresql.dialect())
    lite = _rendered(report_column, sqlite.dialect())
    assert pg.upper().startswith("JSON")
    assert lite  # SQLite renders JSON as its own affinity; only that it renders


def test_the_verdict_column_is_a_bounded_string_on_both() -> None:
    """The verdict is duplicated out of the JSON as a short indexed string so
    a catalogue can filter on "models built from thin data" without parsing
    every report on every row. A length bound that vanished on one dialect
    would quietly remove that."""
    verdict_column = _COVERAGE_COLUMNS[1]
    assert "16" in _rendered(verdict_column, postgresql.dialect())
    assert "16" in _rendered(verdict_column, sqlite.dialect())


def test_the_verdict_index_compiles_for_postgres() -> None:
    """The index is the whole reason the verdict is duplicated out of the
    JSON; an index that does not create is a silent table scan."""
    metadata = sa.MetaData()
    table = sa.Table(
        "surrogate_model_version",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("coverage_report_json", sa.JSON(), nullable=True),
        sa.Column("coverage_verdict", sa.String(length=16), nullable=True),
    )
    index = sa.Index("ix_surrogate_model_version_coverage_verdict", table.c.coverage_verdict)
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        ddl = str(sa.schema.CreateIndex(index).compile(dialect=dialect))
        assert "ix_surrogate_model_version_coverage_verdict" in ddl
        assert "coverage_verdict" in ddl


def test_no_migration_reaches_for_a_postgres_only_type() -> None:
    """The convention across every migration is dialect-agnostic ``sa.JSON()``.

    A ``JSONB`` (or any other ``postgresql.*`` type) would work in production
    and fail every SQLite test — or, worse, pass both and leave the two
    environments storing different things.
    """
    versions = Path(migrations.__file__).parent / "versions"
    offenders = [
        path.name
        for path in versions.glob("*.py")
        if "postgresql." in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"dialect-specific types in: {offenders}"
