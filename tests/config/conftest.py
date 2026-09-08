"""Shared fixtures for matofx.config tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _strip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every MATOFX__* env var so tests run from a clean slate."""
    for key in list(os.environ):
        if key.startswith("MATOFX__") or key == "MATOFX_CONFIG":
            monkeypatch.delenv(key, raising=False)
