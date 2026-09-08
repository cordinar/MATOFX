"""Tests for canonical record hashing."""

from __future__ import annotations

import datetime as _dt

import pytest

from matofx.audit.hashing import GENESIS_HASH, canonical_bytes, compute_hash


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "timestamp": _dt.datetime(2026, 5, 3, 12, 0, 0, 123456, tzinfo=_dt.UTC),
        "actor": "user:42",
        "actor_type": "user",
        "action": "login.success",
        "resource_type": "session",
        "resource_id": "s-1",
        "outcome": "success",
        "request_id": None,
        "session_id": None,
        "source_ip": None,
        "payload": {"foo": "bar"},
        "reason": None,
        "correlation_id": None,
        "key_id": None,
        "prior_hash": GENESIS_HASH,
    }
    base.update(overrides)
    return base


def test_canonical_bytes_deterministic_across_dict_orderings() -> None:
    a = _record(payload={"a": 1, "b": 2})
    b = _record(payload={"b": 2, "a": 1})
    assert canonical_bytes(a) == canonical_bytes(b)


def test_compute_hash_is_64_hex_chars() -> None:
    h = compute_hash(_record())
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_hash_changes_when_any_field_changes() -> None:
    base = compute_hash(_record())
    assert compute_hash(_record(actor="user:99")) != base
    assert compute_hash(_record(payload={"foo": "BAZ"})) != base
    assert compute_hash(_record(prior_hash="ff" * 32)) != base
    assert compute_hash(_record(reason="manual override")) != base


def test_naive_timestamp_treated_as_utc() -> None:
    naive = _dt.datetime(2026, 5, 3, 12, 0, 0, 123456)
    aware = naive.replace(tzinfo=_dt.UTC)
    assert compute_hash(_record(timestamp=naive)) == compute_hash(_record(timestamp=aware))


def test_missing_field_raises_keyerror() -> None:
    record = _record()
    del record["actor"]
    with pytest.raises(KeyError):
        canonical_bytes(record)


def test_genesis_hash_constant() -> None:
    assert GENESIS_HASH == "0" * 64
