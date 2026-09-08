"""Unit tests for ``matofx.migrate.signing``.

Pure-function tests — no DB, no fixtures beyond an in-memory key pair.
The goal is to lock the plain-English error messages and the
canonicalisation behaviour: both are part of the public surface of
the migration feature (the UI quotes the errors verbatim and the
trust-list lookup depends on canonical fingerprints).
"""

from __future__ import annotations

import pytest

from matofx.migrate.signing import (
    FINGERPRINT_HEX_LEN,
    PUBLIC_KEY_HEX_LEN,
    BundleSigningError,
    KeyPair,
    canonicalise_fingerprint,
    compute_fingerprint,
    display_fingerprint,
    fingerprints_equal,
    public_key_from_hex,
    public_key_to_hex,
    sign_manifest,
    verify_manifest,
)


# ---------------------------------------------------------------------------
# Fingerprint canonicalisation
# ---------------------------------------------------------------------------


CANONICAL = "9f2ea17b043d8ce1556a2f97cb0d187c0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "pasted",
    [
        CANONICAL,
        CANONICAL.upper(),
        "9F:2E:A1:7B:04:3D:8C:E1:55:6A:2F:97:CB:0D:18:7C:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF",
        "9f 2e a1 7b 04 3d 8c e1 55 6a 2f 97 cb 0d 18 7c 01 23 45 67 89 ab cd ef 01 23 45 67 89 ab cd ef",
        "9F-2E-A1-7B-04-3D-8C-E1-55-6A-2F-97-CB-0D-18-7C-01-23-45-67-89-AB-CD-EF-01-23-45-67-89-AB-CD-EF",
        f"SHA256:{CANONICAL.upper()}",
        f"sha-256: {CANONICAL}",
        f"fp= {CANONICAL.upper()}",
    ],
)
def test_canonicalise_accepts_common_formats(pasted: str) -> None:
    assert canonicalise_fingerprint(pasted) == CANONICAL


def test_canonicalise_strips_surrounding_whitespace() -> None:
    assert canonicalise_fingerprint(f"   {CANONICAL}\n") == CANONICAL


def test_canonicalise_empty_raises_actionable_error() -> None:
    with pytest.raises(BundleSigningError) as exc_info:
        canonicalise_fingerprint("   ")
    assert "empty" in str(exc_info.value).lower()
    assert "paste" in str(exc_info.value).lower()


def test_canonicalise_non_hex_raises_with_explanation() -> None:
    with pytest.raises(BundleSigningError) as exc_info:
        canonicalise_fingerprint("not-a-fingerprint-just-words-here")
    msg = str(exc_info.value).lower()
    assert "hexadecimal" in msg or "hex" in msg


def test_canonicalise_wrong_length_raises_with_lengths() -> None:
    with pytest.raises(BundleSigningError) as exc_info:
        canonicalise_fingerprint("9f2ea17b")
    msg = str(exc_info.value)
    assert "length" in msg.lower()
    assert str(FINGERPRINT_HEX_LEN) in msg
    assert "8" in msg  # the actual length we passed


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------


def test_display_fingerprint_is_uppercase_colon_separated() -> None:
    rendered = display_fingerprint(CANONICAL)
    assert rendered.startswith("9F:2E:A1:7B")
    assert rendered.count(":") == FINGERPRINT_HEX_LEN // 2 - 1
    # Each pair is two hex chars between colons.
    for pair in rendered.split(":"):
        assert len(pair) == 2
        assert pair == pair.upper()


def test_display_fingerprint_rejects_non_canonical_input() -> None:
    with pytest.raises(BundleSigningError):
        display_fingerprint(CANONICAL.upper())  # uppercase isn't canonical
    with pytest.raises(BundleSigningError):
        display_fingerprint("9f2e")  # too short


def test_canonical_display_round_trip() -> None:
    pasted = display_fingerprint(CANONICAL)
    assert canonicalise_fingerprint(pasted) == CANONICAL


# ---------------------------------------------------------------------------
# fingerprints_equal
# ---------------------------------------------------------------------------


def test_fingerprints_equal_handles_match_and_mismatch() -> None:
    a = "a" * FINGERPRINT_HEX_LEN
    b = "a" * FINGERPRINT_HEX_LEN
    c = "b" * FINGERPRINT_HEX_LEN
    assert fingerprints_equal(a, b)
    assert not fingerprints_equal(a, c)
    assert not fingerprints_equal(a, a[:-1])  # different length


# ---------------------------------------------------------------------------
# Public key encoding
# ---------------------------------------------------------------------------


def test_public_key_hex_round_trip() -> None:
    kp = KeyPair.generate()
    hex_str = public_key_to_hex(kp.public_key)
    assert len(hex_str) == PUBLIC_KEY_HEX_LEN
    restored = public_key_from_hex(hex_str)
    assert public_key_to_hex(restored) == hex_str


def test_public_key_from_hex_rejects_wrong_length() -> None:
    with pytest.raises(BundleSigningError) as exc_info:
        public_key_from_hex("abcd")
    assert "wrong length" in str(exc_info.value).lower()


def test_public_key_from_hex_rejects_non_hex() -> None:
    with pytest.raises(BundleSigningError) as exc_info:
        public_key_from_hex("z" * PUBLIC_KEY_HEX_LEN)
    assert "hexadecimal" in str(exc_info.value).lower()


def test_compute_fingerprint_matches_hash_of_public_key_bytes() -> None:
    import hashlib

    kp = KeyPair.generate()
    expected = hashlib.sha256(bytes.fromhex(public_key_to_hex(kp.public_key))).hexdigest()
    assert compute_fingerprint(kp.public_key) == expected
    assert len(compute_fingerprint(kp.public_key)) == FINGERPRINT_HEX_LEN


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def test_sign_then_verify_succeeds() -> None:
    kp = KeyPair.generate()
    payload = b"hello manifest"
    sig = sign_manifest(payload, kp.private_key)
    assert len(sig) == 64  # Ed25519 signature length
    verify_manifest(payload, sig, kp.public_key)  # does not raise


def test_verify_rejects_tampered_payload() -> None:
    kp = KeyPair.generate()
    sig = sign_manifest(b"hello manifest", kp.private_key)
    with pytest.raises(BundleSigningError) as exc_info:
        verify_manifest(b"hello manifest!", sig, kp.public_key)
    assert "signature does not match" in str(exc_info.value).lower()


def test_verify_rejects_wrong_key() -> None:
    signer = KeyPair.generate()
    attacker = KeyPair.generate()
    sig = sign_manifest(b"manifest", signer.private_key)
    with pytest.raises(BundleSigningError):
        verify_manifest(b"manifest", sig, attacker.public_key)


def test_verify_rejects_corrupted_signature() -> None:
    kp = KeyPair.generate()
    sig = bytearray(sign_manifest(b"manifest", kp.private_key))
    sig[0] ^= 0xFF  # flip a bit
    with pytest.raises(BundleSigningError):
        verify_manifest(b"manifest", bytes(sig), kp.public_key)
