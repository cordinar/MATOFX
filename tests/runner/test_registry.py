"""Tests for the target registry."""

from __future__ import annotations

import pytest

from matofx.runner import TargetRegistry, UnsupportedTargetError
from matofx.tools import LocalTarget


def test_default_registry_has_local_only() -> None:
    registry = TargetRegistry()
    assert isinstance(registry.resolve("local"), LocalTarget)


def test_aos_unsupported_until_registered() -> None:
    registry = TargetRegistry()
    with pytest.raises(UnsupportedTargetError, match="aos"):
        registry.resolve("aos")


def test_register_replaces_existing_target() -> None:
    registry = TargetRegistry()
    new_local = LocalTarget()
    registry.register("local", new_local)
    assert registry.resolve("local") is new_local
