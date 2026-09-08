"""Guard: the Funis issuer public keys stay baked into the build.

`TRUSTED_ISSUER_KEYS` is what makes a licence verifiable. Every failure
mode around it is silent and lands on a customer rather than on us:

- **Emptied** (a "tidy-up", a bad merge, a revert): `issuer_required`
  goes back to ``False``, every build after that quietly accepts unsigned
  licences, and nothing anywhere reports a problem.
- **An id removed** in a later rotation: every licence signed with that
  id stops verifying *retroactively*, including ones already installed.
- **Backup key dropped**, keeping only the working key: the revocation
  path disappears. Nobody notices until the day it is needed, which is
  the worst possible day to find out.
- **A malformed hex value**: fails only at the moment a real licence is
  checked — at a customer's go-live.

None of that is caught by the licence tests, which monkeypatch the map to
whatever they need. Hence a test about the constant itself.

Minted 2026-08-13; custody in `designs/funis-key-custody.md`.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from matofx.compliance.issuer import TRUSTED_ISSUER_KEYS

_ED25519_PUBLIC_KEY_HEX_LEN = 64
"""32 raw bytes, hex-encoded."""

_EXPECTED_IDS = frozenset({"funis-2026-01", "funis-2026-02"})
"""The working and backup ids minted 2026-08-13.

A rotation should **add** ids here, never remove one — old keys stay
trusted until every customer signed under them has re-issued.
"""


def test_the_map_is_populated() -> None:
    """An empty map silently disables issuer verification for every build."""
    assert TRUSTED_ISSUER_KEYS, (
        "TRUSTED_ISSUER_KEYS is empty. A build with no issuer keys accepts "
        "unsigned licences (licence/loader.py:issuer_required) — silently. "
        "If you are deliberately un-baking the keys, delete this test and say "
        "why in the commit."
    )


def test_both_the_working_and_backup_keys_are_present() -> None:
    """Shipping only the working key leaves no revocation path at all.

    The backup is trusted here precisely so it is never used: if the
    working key is compromised, Funis re-issues under the backup and every
    install already in the field accepts the result with no software
    update. Drop it and the only remedy is shipping a new release to
    everybody.
    """
    missing = _EXPECTED_IDS - set(TRUSTED_ISSUER_KEYS)
    assert not missing, f"issuer key id(s) missing from the build: {sorted(missing)}"


@pytest.mark.parametrize("key_id", sorted(_EXPECTED_IDS))
def test_each_key_is_a_valid_ed25519_public_key(key_id: str) -> None:
    """A typo'd hex value would surface at a customer's go-live, not here."""
    value = TRUSTED_ISSUER_KEYS[key_id]
    assert len(value) == _ED25519_PUBLIC_KEY_HEX_LEN, (
        f"{key_id}: expected {_ED25519_PUBLIC_KEY_HEX_LEN} hex chars, got {len(value)}"
    )
    raw = bytes.fromhex(value)  # raises on a non-hex character
    Ed25519PublicKey.from_public_bytes(raw)  # raises on an invalid point encoding


def test_the_keys_are_distinct() -> None:
    """Working and backup must be different keys.

    Pasting the same hex twice satisfies every other check here and looks
    entirely plausible in a diff, while leaving no revocation path — the
    failure this whole pair exists to prevent.
    """
    assert len(set(TRUSTED_ISSUER_KEYS.values())) == len(TRUSTED_ISSUER_KEYS), (
        "duplicate public key in TRUSTED_ISSUER_KEYS — the backup key must differ "
        "from the working key or it is not a spare"
    )


def test_no_placeholder_values_survived() -> None:
    """Catch the obvious paste errors — a doc placeholder or a test stub.

    ``"ab" * 32`` is the value the licence tests monkeypatch in; it is
    exactly what a copy-paste from a test would leave behind, and it is a
    structurally valid Ed25519 public key, so every other check here
    passes it.
    """
    for key_id, value in TRUSTED_ISSUER_KEYS.items():
        assert "<" not in value, f"{key_id}: placeholder text"
        assert ">" not in value, f"{key_id}: placeholder text"
        assert value != "ab" * 32, f"{key_id}: this is the test-stub key, not a real one"
        assert len(set(value)) > 4, f"{key_id}: value looks like filler, not a real key"
