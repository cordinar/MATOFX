"""Shared fixtures for matofx.db tests.

Provides ``db_settings`` parametrised over both supported dialects:

- SQLite — always available; uses a temp file.
- Postgres — requires ``MATOFX_TEST_POSTGRES_URL`` to be set; otherwise
  the parametrised case is skipped.

Each test function that takes ``db_settings`` runs once per available
dialect.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from matofx.config import DbSettings


def _postgres_url() -> str | None:
    return os.environ.get("MATOFX_TEST_POSTGRES_URL")


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def db_settings(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[DbSettings]:
    """Yield a DbSettings for each dialect we can exercise on this host."""
    dialect: str = request.param
    if dialect == "sqlite":
        url = f"sqlite:///{tmp_path / 'test.db'}"
        yield DbSettings.from_url(url, dialect="sqlite")
    else:
        pg_url = _postgres_url()
        if not pg_url:
            pytest.skip("MATOFX_TEST_POSTGRES_URL not set")
        yield DbSettings.from_url(pg_url, dialect="postgres")
