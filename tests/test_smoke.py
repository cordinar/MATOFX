"""Smoke test — confirms the package imports and CI is wired up."""

import matofx


def test_version_string() -> None:
    assert isinstance(matofx.__version__, str)
    assert matofx.__version__
