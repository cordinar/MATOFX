"""The "Pick from library" draft-field reference helpers (ADR-0006 §6).

Pure unit tests (no DB) for the sentinel that lets a library pick flow
through the str-typed form pipeline and be resolved at submit.
"""

from __future__ import annotations

from matofx.library.refs import (
    LIBRARY_REF_PREFIX,
    build_library_ref,
    is_library_ref,
    parse_library_ref,
)


def test_build_then_parse_round_trips() -> None:
    ref = build_library_ref(42)
    assert ref == f"{LIBRARY_REF_PREFIX}42"
    assert is_library_ref(ref)
    assert parse_library_ref(ref) == 42


def test_parse_accepts_dict_form() -> None:
    assert parse_library_ref({"source": "library", "version_id": 7}) == 7
    assert parse_library_ref({"source": "library", "version_id": "7"}) == 7


def test_parse_rejects_non_references() -> None:
    # Plain paths / previous-job picks / typed values pass through as None.
    assert parse_library_ref("/jobs/1/inputs/part.step") is None
    assert parse_library_ref("") is None
    assert parse_library_ref(None) is None
    assert parse_library_ref(123) is None
    assert parse_library_ref({"source": "upload", "version_id": 7}) is None
    assert not is_library_ref("/some/path.stp")


def test_parse_rejects_malformed_sentinel_and_bool() -> None:
    assert parse_library_ref(f"{LIBRARY_REF_PREFIX}notanint") is None
    # bool is an int subclass — must not be coerced to a version id.
    assert parse_library_ref({"source": "library", "version_id": True}) is None
    assert parse_library_ref({"source": "library", "version_id": None}) is None
