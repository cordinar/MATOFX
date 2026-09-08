"""Syntax-validation for in-browser file edits (Session C C2).

The Files tab's Save action runs every edited file through this
module *before* the bundle copy + write happen, so a bundle that
won't even parse never lands on disk and never bumps the tool's
version. Broken edits surface as :class:`SyntaxValidationError`
which the API maps to a 422 with a structured per-line errors list
the UI renders inline.

Per-language coverage:

- ``.py`` / ``.pyi`` — ``compile(...)`` catches every ``SyntaxError``,
  ``IndentationError``, and ``TabError`` (a strict superset of
  ``ast.parse``).
- ``.json`` — :func:`json.loads`.
- ``.yml`` / ``.yaml`` — :func:`yaml.safe_load` (PyYAML 6.x; pinned
  in the project's requirements).
- ``.toml`` — :func:`tomllib.loads` (Python 3.11+ stdlib).
- Anything else (Markdown, plain text, the assorted rc files) is
  passed through unchecked — there's no canonical parser to flag a
  "syntax error" against.

This module is deliberately a leaf: no DB, no I/O, no service-layer
dependencies. That keeps :func:`check_syntax` cheap to call from
both the service layer (during apply_file_edit) and any future
"Check syntax" button on the editor.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class SyntaxIssue:
    """One parser diagnostic: where it is, what's wrong, plain English.

    Lines and columns are 1-indexed (matches the editor's gutter).
    ``column`` may be ``0`` when the parser couldn't pinpoint a column
    (rare but possible for stream-level YAML errors).
    """

    line: int
    column: int
    message: str

    def to_dict(self) -> dict[str, int | str]:
        """Serialise to the shape the API hands back to the UI."""
        return {"line": self.line, "column": self.column, "message": self.message}


def check_syntax(rel_path: str, contents: str) -> list[SyntaxIssue]:
    """Return any syntax errors found in ``contents``. Empty list = OK.

    ``rel_path`` is the bundle-relative path; only its extension is
    consulted to choose a parser.
    """
    suffix = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
    checker = _CHECKERS.get(suffix)
    if checker is None:
        return []
    return checker(rel_path, contents)


def _check_python(rel_path: str, contents: str) -> list[SyntaxIssue]:
    """Run the file through ``compile`` to catch every Python parse error.

    ``compile`` reports line + offset for ``SyntaxError`` /
    ``IndentationError`` / ``TabError``; we surface them as a
    single-issue list since Python's parser bails on the first.
    """
    try:
        compile(contents, rel_path, "exec")
    except SyntaxError as exc:
        line = exc.lineno or 1
        column = exc.offset or 0
        message = exc.msg or "syntax error"
        return [SyntaxIssue(line=line, column=column, message=message)]
    return []


def _check_json(rel_path: str, contents: str) -> list[SyntaxIssue]:
    """Validate JSON via :func:`json.loads`."""
    del rel_path
    try:
        json.loads(contents)
    except json.JSONDecodeError as exc:
        return [SyntaxIssue(line=exc.lineno, column=exc.colno, message=exc.msg)]
    return []


def _check_yaml(rel_path: str, contents: str) -> list[SyntaxIssue]:
    """Validate YAML via :func:`yaml.safe_load`."""
    del rel_path
    try:
        yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        line = 1
        column = 0
        # PyYAML's MarkedYAMLError carries a problem_mark with line/col
        # (0-indexed); fall back to 1/0 for parser-level errors that
        # don't pinpoint a position.
        problem_mark = getattr(exc, "problem_mark", None)
        if problem_mark is not None:
            line = int(problem_mark.line) + 1
            column = int(problem_mark.column) + 1
        problem = getattr(exc, "problem", None) or str(exc)
        return [SyntaxIssue(line=line, column=column, message=str(problem))]
    return []


def _check_toml(rel_path: str, contents: str) -> list[SyntaxIssue]:
    """Validate TOML via :func:`tomllib.loads` (stdlib, 3.11+)."""
    del rel_path
    try:
        tomllib.loads(contents)
    except tomllib.TOMLDecodeError as exc:
        # tomllib's exception text is plain English with the position
        # embedded; we don't get programmatic line/col so we surface
        # the message and let the UI render it as a single line.
        return [SyntaxIssue(line=1, column=0, message=str(exc))]
    return []


_CHECKERS = {
    ".py": _check_python,
    ".pyi": _check_python,
    ".json": _check_json,
    ".yml": _check_yaml,
    ".yaml": _check_yaml,
    ".toml": _check_toml,
}
