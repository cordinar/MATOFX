"""Smoke tests for the ``matofx migrate ...`` CLI subcommands.

Two layers:

* **Argparse-level**: usage / help / missing-subcommand exit codes.
  These don't touch the DB or the filesystem — they verify the parser
  wiring stays correct and required flags are enforced.
* **Helper-level**: the pure ``_parse_tool_specs`` / ``_parse_alias_list``
  / ``_format_event`` helpers used by the dispatchers.

The DB-side service behaviour (export, import, trust mutations) is
exhaustively covered by ``tests/migrate/`` and ``tests/api/`` — the
CLI is a thin shell over those services, so we don't re-run the same
scenarios through subprocess here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from matofx.cli.main import (
    _format_event,
    _parse_alias_list,
    _parse_tool_specs,
    main,
)


# ---------------------------------------------------------------------------
# Argparse / dispatch wiring
# ---------------------------------------------------------------------------


def test_migrate_no_subcommand_returns_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["migrate"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "matofx migrate" in err
    assert "fingerprint" in err and "trust" in err


def test_migrate_trust_no_subcommand_returns_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["migrate", "trust"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "trust" in err
    assert "list" in err and "add" in err and "remove" in err


def test_migrate_help_lists_every_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["migrate", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for subcommand in ("fingerprint", "trust", "export", "import"):
        assert subcommand in out


def test_trust_add_requires_user(capsys: pytest.CaptureFixture[str]) -> None:
    """Missing ``--user`` is a parse error (argparse exits with 2)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["migrate", "trust", "add", "abcd", "--label", "x"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--user" in err


def test_export_requires_user_and_out(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["migrate", "export", "--tools", "alpha:1.0.0"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    # Either --out or --user — whichever argparse mentions first.
    assert "--out" in err or "--user" in err


def test_import_requires_user(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["migrate", "import", "/tmp/bundle.tar.gz"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--user" in err


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_tool_specs_happy_path() -> None:
    assert _parse_tool_specs("alpha:1.0.0,beta:2.1") == [
        ("alpha", "1.0.0"),
        ("beta", "2.1"),
    ]


def test_parse_tool_specs_handles_whitespace_and_blank_entries() -> None:
    assert _parse_tool_specs("  alpha:1.0  , , beta:2.0 ") == [
        ("alpha", "1.0"),
        ("beta", "2.0"),
    ]


def test_parse_tool_specs_empty_returns_empty() -> None:
    assert _parse_tool_specs("") == []
    assert _parse_tool_specs(None) == []  # type: ignore[arg-type]


def test_parse_tool_specs_missing_colon_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_tool_specs("alpha")
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "alias:version" in err


def test_parse_alias_list() -> None:
    assert _parse_alias_list("alpha,beta,gamma") == ["alpha", "beta", "gamma"]
    assert _parse_alias_list("  alpha , , beta  ") == ["alpha", "beta"]
    assert _parse_alias_list("") == []


@dataclass
class _FakeEvent:
    level: str
    message: str


@pytest.mark.parametrize(
    "level,expected_prefix",
    [
        ("info", "  "),
        ("imported", "OK"),
        ("skipped", "SKIP"),
        ("warning", "WARN"),
        ("error", "ERR"),
        ("summary", "---"),
        ("nonsense", "  "),  # unknown level → neutral indent
    ],
)
def test_format_event_prefixes_by_level(level: str, expected_prefix: str) -> None:
    rendered = _format_event(_FakeEvent(level=level, message="hello"))
    assert rendered.startswith(expected_prefix)
    assert "hello" in rendered
