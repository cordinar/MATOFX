"""Reading a growing log without corrupting it.

The live job page polls its logs every couple of seconds. Before tail
mode it re-read and re-sent the whole file each time — fine for the
one-line logs ENV-A exercised, untenable for a CFD run writing hundreds
of megabytes to stdout on a box that is also running the solver.

Tail mode is only interesting because of what it must *not* break:

- a character must never be split across two polls (UTF-8 is
  multi-byte, and a solver printing degree signs will eventually put one
  on a chunk boundary);
- a finished job's last line must survive even if nothing wrote the
  trailing newline — that is exactly the line a crash leaves behind.

The two requirements pull in opposite directions, which is why ``tail``
is a parameter rather than always-on. These tests pin both sides.
"""

from __future__ import annotations

from pathlib import Path

from matofx.api.routers.jobs import _MAX_UNTERMINATED_BYTES, _read_log_blocking

# 'é' is two bytes in UTF-8 (0xC3 0xA9) — the boundary hazard.
CAFE = "café\n"


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


# ---- whole-file mode (a finished job) -------------------------------


def test_whole_file_read_returns_everything(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    _write(log, b"first\nsecond\n")
    text, offset, restarted = _read_log_blocking(log, 0, tail=False)
    assert text == "first\nsecond\n"
    assert offset == 13
    assert restarted is False


def test_whole_file_read_keeps_a_line_with_no_trailing_newline(tmp_path: Path) -> None:
    """A crash message is usually the line nobody terminated."""
    log = tmp_path / "stdout.log"
    _write(log, b"running\nSegmentation fault")
    text, _offset, _restarted = _read_log_blocking(log, 0, tail=False)
    assert text == "running\nSegmentation fault"


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    text, offset, restarted = _read_log_blocking(tmp_path / "nope.log", 40, tail=False)
    assert (text, offset, restarted) == ("", 40, False)


# ---- tail mode (a running job) --------------------------------------


def test_tail_holds_back_an_unterminated_line(tmp_path: Path) -> None:
    """Half a line is not shown; it arrives whole on the next poll."""
    log = tmp_path / "stdout.log"
    _write(log, b"complete line\npartial")
    text, offset, _restarted = _read_log_blocking(log, 0, tail=True)
    assert text == "complete line\n"
    assert offset == 14  # just past the newline, not past "partial"


def test_tail_resumes_from_the_offset_and_repeats_nothing(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    _write(log, b"one\ntwo\n")
    first, offset, _ = _read_log_blocking(log, 0, tail=True)
    assert first == "one\ntwo\n"
    _write(log, b"one\ntwo\nthree\n")
    second, offset2, _ = _read_log_blocking(log, offset, tail=True)
    assert second == "three\n"
    assert offset2 == 14


def test_tail_never_splits_a_multibyte_character(tmp_path: Path) -> None:
    """The whole reason chunks end on a newline.

    The file is caught mid-write with only the first byte of 'é'
    present. A naive ``read().decode(errors='replace')`` would emit
    ``'caf�'`` here and then ``'�\\n'`` next time — one
    character rendered as two replacement marks, permanently, in a log
    the user is reading to find out why their run failed.
    """
    log = tmp_path / "stdout.log"
    _write(log, b"caf\xc3")  # 'é' half-written, no newline
    text, offset, _ = _read_log_blocking(log, 0, tail=True)
    assert text == ""
    assert offset == 0

    _write(log, CAFE.encode("utf-8"))  # the write completes
    text, offset, _ = _read_log_blocking(log, offset, tail=True)
    assert text == "café\n"
    assert "�" not in text
    assert offset == 6


def test_tail_gives_up_holding_back_a_pathologically_long_line(tmp_path: Path) -> None:
    """A progress bar redrawing with carriage returns writes no newline.

    Without this escape hatch the view would stay empty for the whole
    run while the "line" grew without bound.
    """
    log = tmp_path / "stdout.log"
    blob = b"x" * (_MAX_UNTERMINATED_BYTES + 10)
    _write(log, blob)
    text, offset, _ = _read_log_blocking(log, 0, tail=True)
    assert len(text) == len(blob)
    assert offset == len(blob)


def test_nothing_new_returns_nothing_and_holds_the_offset(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    _write(log, b"done\n")
    text, offset, restarted = _read_log_blocking(log, 5, tail=True)
    assert (text, offset, restarted) == ("", 5, False)


# ---- rotation / re-run ----------------------------------------------


def test_a_shrunken_log_restarts_from_zero_and_says_so(tmp_path: Path) -> None:
    """Appending across a rotation would splice two runs into one view."""
    log = tmp_path / "stdout.log"
    _write(log, b"fresh run\n")
    text, offset, restarted = _read_log_blocking(log, 900, tail=True)
    assert text == "fresh run\n"
    assert offset == 10
    assert restarted is True
