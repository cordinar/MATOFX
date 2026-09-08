"""The Funis-side ledger, canonical bytes, and the human-readable twin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matofx.compliance import LedgerError, render_report_html
from matofx.compliance.canonical import canonical_bytes, content_sha256
from matofx.compliance.ledger import (
    LedgerEntry,
    append_entry,
    entry_from_report,
    known_public_key,
    latest_for,
    read_ledger,
)

# ----- canonical bytes ------------------------------------------------


def test_canonical_bytes_ignore_key_order() -> None:
    """Two orderings of the same document must sign identically."""
    first = {"b": 1, "a": {"y": 2, "x": 3}}
    second = {"a": {"x": 3, "y": 2}, "b": 1}
    assert canonical_bytes(first) == canonical_bytes(second)


def test_signature_field_is_excluded() -> None:
    """Otherwise a document could never be verified after being signed."""
    document = {"a": 1}
    signed = {"a": 1, "signature": {"value": "deadbeef"}}
    assert canonical_bytes(document) == canonical_bytes(signed)


def test_null_and_absent_are_different() -> None:
    """A dropped field must not hash the same as an explicit null."""
    assert canonical_bytes({"a": 1, "b": None}) != canonical_bytes({"a": 1})


def test_content_sha256_is_stable() -> None:
    document = {"counter": 2, "install": {"install_id": "inst_X"}}
    assert content_sha256(document) == content_sha256(dict(reversed(list(document.items()))))


# ----- ledger ---------------------------------------------------------


def _entry(**overrides: object) -> LedgerEntry:
    fields: dict[str, object] = {
        "install_id": "inst_A",
        "counter": 1,
        "head_id": 100,
        "head_hash": "aa" * 32,
    }
    fields.update(overrides)
    return LedgerEntry(**fields)  # type: ignore[arg-type]


def test_missing_ledger_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_ledger(tmp_path / "nothing.jsonl") == []


def test_round_trip_append_and_read(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry())
    append_entry(path, _entry(counter=2, head_id=200, head_hash="bb" * 32))
    entries = read_ledger(path)
    assert [e.counter for e in entries] == [1, 2]
    assert latest_for(entries, "inst_A") is not None
    assert latest_for(entries, "inst_A").counter == 2  # type: ignore[union-attr]


def test_latest_for_isolates_installs(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry(counter=9))
    append_entry(path, _entry(install_id="inst_B", counter=2))
    entries = read_ledger(path)
    assert latest_for(entries, "inst_B").counter == 2  # type: ignore[union-attr]
    assert latest_for(entries, "inst_MISSING") is None


def test_malformed_line_raises_rather_than_being_skipped(tmp_path: Path) -> None:
    """Skipping a bad line could hide the entry that contradicts a report."""
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    with pytest.raises(LedgerError, match="not valid JSON"):
        read_ledger(path)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    path.write_text(json.dumps({"install_id": "inst_A", "counter": 1}) + "\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="missing field"):
        read_ledger(path)


def test_known_public_key_prefers_the_latest_pin(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry(counter=1, report_public_key="old"))
    append_entry(path, _entry(counter=2, report_public_key=None))
    append_entry(path, _entry(counter=3, report_public_key="new"))
    assert known_public_key(read_ledger(path), "inst_A") == "new"


def test_entry_from_report_extracts_the_anchor() -> None:
    document = {
        "counter": 4,
        "generated_at": "2026-08-31T18:04:11.000000Z",
        "install": {"install_id": "inst_A"},
        "anchor": {
            "head": {"id": 91004, "hash": "3c" * 32},
            "genesis_record": {"id": 1, "hash": "0a" * 32},
        },
    }
    entry = entry_from_report(document, public_key="pk", content_sha256="sha")
    assert (entry.install_id, entry.counter, entry.head_id) == ("inst_A", 4, 91004)
    assert entry.genesis_hash == "0a" * 32
    assert entry.report_public_key == "pk"
    assert entry.received_at is not None


# ----- HTML twin ------------------------------------------------------


def _document() -> dict[str, object]:
    return {
        "report_schema_version": 1,
        "generated_at": "2026-08-31T18:04:11.000000Z",
        "counter": 2,
        "install": {
            "install_id": "inst_A",
            "customer": "Northwind <Aerospace> Ltd",
            "environment": "production",
            "app_version": "1.0.3",
            "db_dialect": "postgres",
            "issuer_key_id": "funis-2026-01",
            "issuer_verified": True,
        },
        "licence": {
            "state": "active",
            "licence_id": "LIC-1",
            "licence_type": "csl",
            "n_slots": 10,
            "expires_at": "2027-01-14T00:00:00Z",
            "features_enabled": ["parametric_studies"],
        },
        "period": {"from": "2026-08-01T00:00:00Z", "to": "2026-08-31T18:04:11Z"},
        "anchor": {
            "genesis_record": {"id": 1, "hash": "0a" * 32},
            "previous_report": {"counter": 1, "head_id": 84120, "head_hash": "a9" * 32},
            "previous_head_rehashed_now": "a9" * 32,
            "head": {"id": 91004, "hash": "3c" * 32},
        },
        "chain_verification": {"ok": True, "records_checked": 91004, "failures": []},
        "usage": {
            "licence_type": "csl",
            "authoritative": "csl",
            "csl": {
                "n_slots": 10,
                "peak_concurrent_sessions": 11,
                "over_cap": True,
                "sessions_started": 210,
                "slots_granted": 2118,
                "slots_denied": 14,
                "breakglass_admissions": 1,
                "peak_by_day": {"2026-08-14": 11},
            },
            "fal": {
                "n_slots": None,
                "assignments_current": 41,
                "over_cap": None,
                "assignments_granted": 0,
                "assignments_revoked": 0,
                "assignments_denied": 0,
                "registered_users": 63,
                "source": "enabled_accounts",
            },
        },
        "licence_events": {"licence.slot.breakglass": 1},
        "notable_events": [
            {
                "record_id": 88431,
                "timestamp": "2026-08-14T09:12:55Z",
                "action": "licence.slot.breakglass",
                "actor": "super_user:1",
                "outcome": "success",
                "reason": "revoking stale session",
            }
        ],
        "notable_events_truncated": False,
        "signature": {"alg": "ed25519", "key_id": "3f9a1d", "value": "ab" * 32},
    }


def test_html_twin_is_self_contained_and_carries_the_facts() -> None:
    html = render_report_html(_document())
    assert "<!doctype html>" in html
    # No external assets — it has to open on an air-gapped machine.
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html
    for expected in ("inst_A", "11 / 10", "over cap", "84120", "super_user:1", "3f9a1d"):
        assert expected in html


def test_html_twin_escapes_customer_supplied_text() -> None:
    html = render_report_html(_document())
    assert "Northwind &lt;Aerospace&gt; Ltd" in html
    assert "<Aerospace>" not in html


def test_html_twin_labels_the_non_authoritative_block() -> None:
    """FAL figures are shown, but marked as observation under a CSL licence."""
    html = render_report_html(_document())
    assert "authoritative" in html
    assert "observation" in html


def test_html_twin_renders_a_missing_previous_head_loudly() -> None:
    document = _document()
    document["anchor"]["previous_head_rehashed_now"] = None  # type: ignore[index]
    assert "MISSING" in render_report_html(document)
