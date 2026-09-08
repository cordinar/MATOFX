"""Slice 0 — the ``SummaryFacts`` grounding contract.

Pins the JSON-serialisability, the deterministic canonical hash, and the
shared narrative phrasing helpers. These invariants are what the cache key
(facts-hash invalidation) and the enrichment guard stack rely on.
"""

from __future__ import annotations

import json

from matofx.summaries import (
    SURFACE_KINDS,
    TERMINAL_STATES,
    HeadlineNumber,
    SummaryFacts,
    SummaryText,
    is_terminal,
)
from matofx.summaries.narrative import format_number, humanise_duration, join_list


def _facts(**overrides: object) -> SummaryFacts:
    base: dict[str, object] = {
        "surface_kind": "optimisation_study",
        "surface_id": 312,
        "title": "Heat-sink lightweighting",
        "state": "complete",
        "headline": (
            HeadlineNumber("Mass", 0.149, "kg"),
            HeadlineNumber("Peak temperature", 98.7, "°C"),
        ),
        "submitted_by": "robert@funisoft.com",
        "duration_seconds": 1057.0,
        "details": {"strategy": "NSGA-II", "evaluations": 120},
    }
    base.update(overrides)
    return SummaryFacts(**base)  # type: ignore[arg-type]


def test_to_dict_is_json_serialisable() -> None:
    facts = _facts()
    encoded = json.dumps(facts.to_dict())
    decoded = json.loads(encoded)
    assert decoded["surface_kind"] == "optimisation_study"
    assert decoded["headline"][0] == {"label": "Mass", "value": 0.149, "unit": "kg"}
    assert decoded["details"]["strategy"] == "NSGA-II"


def test_facts_hash_is_stable_across_instances() -> None:
    """Two equal fact sets hash identically — the cache-key contract."""
    assert _facts().facts_hash() == _facts().facts_hash()


def test_facts_hash_is_insensitive_to_details_key_order() -> None:
    a = _facts(details={"strategy": "NSGA-II", "evaluations": 120})
    b = _facts(details={"evaluations": 120, "strategy": "NSGA-II"})
    assert a.facts_hash() == b.facts_hash()


def test_facts_hash_changes_when_a_number_changes() -> None:
    """Any fact change busts the hash — how a stale summary invalidates."""
    base = _facts()
    changed = _facts(
        headline=(
            HeadlineNumber("Mass", 0.150, "kg"),
            HeadlineNumber("Peak temperature", 98.7, "°C"),
        )
    )
    assert base.facts_hash() != changed.facts_hash()


def test_facts_hash_is_64_hex_chars() -> None:
    digest = _facts().facts_hash()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_surface_kinds_and_terminal_states() -> None:
    assert set(SURFACE_KINDS) == {
        "job",
        "workflow_run",
        "parametric_study",
        "optimisation_study",
    }
    assert is_terminal("complete")
    assert is_terminal("failed")
    assert not is_terminal("running")
    assert "cancelled" in TERMINAL_STATES


def test_summary_text_round_trip() -> None:
    text = SummaryText(title="Summary", paragraphs=("One.", "Two."))
    assert SummaryText.from_dict(text.to_dict()) == text


def test_format_number() -> None:
    assert format_number(98.7, "°C") == "98.7 °C"
    assert format_number(0.14912345, "kg") == "0.1491 kg"
    assert format_number(120) == "120"
    assert format_number(None) == "not available"
    assert format_number("converged") == "converged"


def test_humanise_duration() -> None:
    assert humanise_duration(42) == "42 s"
    assert humanise_duration(252) == "4 min 12 s"
    assert humanise_duration(660) == "11 min"
    assert humanise_duration(7380) == "2 h 03 min"
    assert humanise_duration(None) == "an unknown time"


def test_join_list() -> None:
    assert join_list(["a"]) == "a"
    assert join_list(["a", "b"]) == "a and b"
    assert join_list(["a", "b", "c"]) == "a, b and c"
    assert join_list([]) == ""
