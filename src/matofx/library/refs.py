"""The "Pick from library" draft-field reference (ADR-0006 §6).

A tool-form file input set to the library source carries a small string
**sentinel** as its draft value so it flows through the form pipeline —
which types file fields as plain strings (``tools/forms/model``) and
two-way-binds the widget to ``state[field]`` — completely unchanged. At
submit, :meth:`JobService._resolve_library_inputs` resolves the sentinel
into the chosen version's bytes (staged through the normal upload path)
and writes the ``job_library_file_use`` provenance row.

A string sentinel (rather than a nested dict) is what lets the value
survive the str-typed form pipeline without special-casing every widget
and validator. :func:`parse_library_ref` also accepts the equivalent
dict form (``{"source": "library", "version_id": N}``) so a caller that
sets the draft value directly (e.g. a future API path) still resolves.

This module is intentionally dependency-free (stdlib only) so the three
packages that touch it — :mod:`matofx.tools.forms`, the UI form widget,
and :mod:`matofx.hierarchy` — can import it without any import cycle.
"""

from __future__ import annotations

from typing import Any

LIBRARY_REF_PREFIX = "matofx-library://"
"""Scheme-like prefix for the draft sentinel; ``matofx-library://<version_id>``.

Deliberately not a valid filesystem path (or accepted-extension match)
so it can never collide with a real uploaded / typed file path."""


def build_library_ref(version_id: int) -> str:
    """Encode a chosen library-file-version id as a draft-field sentinel."""
    return f"{LIBRARY_REF_PREFIX}{int(version_id)}"


def is_library_ref(value: object) -> bool:
    """``True`` when ``value`` is the string sentinel (cheap widget check)."""
    return isinstance(value, str) and value.startswith(LIBRARY_REF_PREFIX)


def parse_library_ref(value: object) -> int | None:
    """Return the library version id a draft file value encodes, or ``None``.

    Accepts the string sentinel (``matofx-library://<id>``) and the
    equivalent dict form (``{"source": "library", "version_id": N}``).
    Anything else — a typed path, an uploaded path, a previous-job pick —
    returns ``None`` and is left untouched by the submit resolver.
    """
    if isinstance(value, str):
        return _parse_str_ref(value)
    if isinstance(value, dict) and value.get("source") == "library":
        return _coerce_version_id(value.get("version_id"))
    return None


def _parse_str_ref(value: str) -> int | None:
    if not value.startswith(LIBRARY_REF_PREFIX):
        return None
    try:
        return int(value[len(LIBRARY_REF_PREFIX) :])
    except ValueError:
        return None


def _coerce_version_id(vid: Any) -> int | None:
    if vid is None or isinstance(vid, bool):
        return None
    try:
        return int(vid)
    except (TypeError, ValueError):
        return None
