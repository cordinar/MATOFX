"""The ``matofx licence ...`` CLI, and the operator-facing failures.

Both tests here exist because of a real defect found while running the
issuing flow end to end, and both guard the same thing: **the message an
administrator reads at go-live**.

The licence subsystem's error messages are carefully written — they name
the offending install id, the expected one, and the command that fixes
it. That work is wasted if the message arrives at the bottom of a Python
stack trace, so the rendering is worth pinning as tightly as the logic.

DB-side behaviour is covered by ``tests/licence/``; this is the shell.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from matofx.cli import main as cli_main
from matofx.cli.main import main
from matofx.licence.errors import LicenceInvalidError

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The vendor-side issuer. Funis keeps it in its own workspace; the product
#: repo doesn't ship it.
ISSUE_LICENCE = REPO_ROOT / "scripts" / "issue_licence.py"


# ---------------------------------------------------------------------------
# Argparse / dispatch wiring
# ---------------------------------------------------------------------------


def test_licence_no_subcommand_returns_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["licence"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "install" in err
    assert "reload" in err
    assert "show" in err
    assert "request" in err


def test_licence_help_lists_request(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["licence", "--help"])
    assert exc_info.value.code == 0
    assert "request" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# A refused licence must read as a sentence, not a stack
# ---------------------------------------------------------------------------


def test_licence_error_prints_the_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A licence issued for another install is refused *inside*
    ``install_licence`` — past the pre-copy check the handler catches
    itself — so before this was fixed the administrator got 25 lines of
    asyncio stack with the actionable sentence at the very bottom.
    """
    message = (
        "This licence was issued for install 'inst_OTHER', but this is "
        "install 'inst_MINE'. Run 'matofx licence request' and ask your "
        "supplier to issue a licence for this install."
    )

    def _boom(_parsed: argparse.Namespace) -> int:
        raise LicenceInvalidError(message)

    monkeypatch.setattr(cli_main, "_dispatch_licence", _boom)

    rc = main(["licence", "show"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == f"matofx: {message}"
    # The guard is the *absence* of the stack, so assert on it directly:
    # a passing happy path proves nothing here.
    assert "Traceback" not in captured.err
    assert "asyncio" not in captured.err


# ---------------------------------------------------------------------------
# The issuing script must survive a Windows console
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ISSUE_LICENCE.exists(), reason="the licence issuer isn't in this repo")
def test_issuing_script_output_survives_a_cp1252_console() -> None:
    """``scripts/issue_licence.py`` writes the licence and saves the
    registry *before* printing its summary. A ``UnicodeEncodeError`` in
    that summary therefore reported failure on an issue that had fully
    succeeded — and the natural response, running it again, mints a
    second licence against the same install.

    A single ``→`` in one f-string did exactly that. Windows
    consoles are cp1252, and Funis issues from one.
    """
    source = ISSUE_LICENCE.read_text(encoding="utf-8")

    offenders = sorted(
        {char for char in source if not _encodable(char)},
    )
    assert not offenders, (
        f"scripts/issue_licence.py contains {offenders!r}, which cannot be "
        "printed on a cp1252 console. Use ASCII in operator-facing output."
    )


def _encodable(char: str) -> bool:
    try:
        char.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True
