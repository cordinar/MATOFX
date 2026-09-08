"""Tests for the config-file path resolution helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from matofx.config.paths import candidate_paths, env_path, system_path, user_path


def test_env_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATOFX_CONFIG", raising=False)
    assert env_path() is None


def test_env_path_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = str(tmp_path / "m.toml")
    monkeypatch.setenv("MATOFX_CONFIG", candidate)
    p = env_path()
    assert p is not None
    assert str(p) == candidate


def test_system_path_platform_aware() -> None:
    p = system_path()
    if sys.platform == "win32":
        assert "matofx.toml" in p.name
        assert "matofx" in str(p).lower()
    else:
        assert str(p) == "/etc/matofx/matofx.toml"


def test_user_path_platform_aware() -> None:
    p = user_path()
    assert p.name == "matofx.toml"
    if sys.platform != "win32":
        assert ".config" in p.parts


def test_candidate_paths_includes_env_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATOFX_CONFIG", "/explicit/from/env.toml")
    paths = candidate_paths()
    assert any(str(p).endswith("env.toml") for p in paths)
    assert paths[0] == env_path()


def test_candidate_paths_excludes_env_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATOFX_CONFIG", raising=False)
    paths = candidate_paths()
    assert all(str(p).find("MATOFX_CONFIG") == -1 for p in paths)
    assert paths == [system_path(), user_path()]
