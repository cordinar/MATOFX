"""Smoke tests for the matofx CLI's audit verify subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from matofx.cli.main import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "matofx" in out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "matofx" in out


def test_audit_without_subcommand_returns_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["audit"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "audit verify" in err


def test_rotate_without_subcommand_returns_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["rotate"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "rotate signing-key" in err


def test_rotate_signing_key_init_with_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keyfile = tmp_path / "audit.key"
    rc = main(["rotate", "signing-key", "--init", "--path", str(keyfile)])
    assert rc == 0
    assert keyfile.is_file()
    out = capsys.readouterr().out
    assert "Initialised audit signing key" in out


def test_rotate_signing_key_init_refuses_existing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keyfile = tmp_path / "audit.key"
    assert main(["rotate", "signing-key", "--init", "--path", str(keyfile)]) == 0
    rc = main(["rotate", "signing-key", "--init", "--path", str(keyfile)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--init refused" in err


def test_rotate_signing_key_rotates_existing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keyfile = tmp_path / "audit.key"
    assert main(["rotate", "signing-key", "--init", "--path", str(keyfile)]) == 0
    capsys.readouterr()
    rc = main(["rotate", "signing-key", "--path", str(keyfile)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Rotated audit signing key" in out
    assert "retired" in out
