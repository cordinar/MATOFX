"""Tests for session-token generation and hashing."""

from __future__ import annotations

from matofx.users import hash_session_token, new_session_token


def test_token_is_long_and_unique() -> None:
    a = new_session_token()
    b = new_session_token()
    assert a != b
    assert len(a) >= 32


def test_hash_is_deterministic_and_64_hex() -> None:
    token = "fixed-string"
    h1 = hash_session_token(token)
    h2 = hash_session_token(token)
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_different_tokens_hash_differently() -> None:
    assert hash_session_token("a") != hash_session_token("b")
