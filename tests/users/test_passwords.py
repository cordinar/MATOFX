"""Tests for password hashing and validation."""

from __future__ import annotations

import pytest

from matofx.config import AuthSettings
from matofx.users import make_password_hasher, validate_password


def _settings(min_length: int = 8) -> AuthSettings:
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=min_length,
    )


def test_hash_then_verify_round_trip() -> None:
    h = make_password_hasher(_settings())
    hashed = h.hash("correct horse battery staple")
    assert h.verify(hashed, "correct horse battery staple") is True


def test_verify_wrong_password_returns_false() -> None:
    h = make_password_hasher(_settings())
    hashed = h.hash("right")
    assert h.verify(hashed, "wrong") is False


def test_verify_invalid_hash_returns_false_not_raises() -> None:
    h = make_password_hasher(_settings())
    assert h.verify("not-a-real-hash", "anything") is False


def test_distinct_hashes_for_same_password() -> None:
    """Argon2id uses a per-hash random salt; two hashes of the same string differ."""
    h = make_password_hasher(_settings())
    a = h.hash("same")
    b = h.hash("same")
    assert a != b
    assert h.verify(a, "same")
    assert h.verify(b, "same")


def test_validate_password_short_rejected() -> None:
    settings = _settings(min_length=12)
    reason = validate_password("short", settings)
    assert reason is not None
    assert "12" in reason


def test_validate_password_at_threshold_accepted() -> None:
    settings = _settings(min_length=8)
    assert validate_password("a" * 8, settings) is None


@pytest.mark.parametrize("password", ["a" * 8, "a" * 100, "Pässwort1234"])
def test_validate_password_accepts_long_enough(password: str) -> None:
    settings = _settings(min_length=8)
    assert validate_password(password, settings) is None


def test_needs_rehash_false_for_freshly_hashed() -> None:
    h = make_password_hasher(_settings())
    hashed = h.hash("x")
    assert h.needs_rehash(hashed) is False
