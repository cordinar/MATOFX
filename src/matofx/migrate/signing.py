"""Ed25519 signing primitives + fingerprint canonicalisation for migration bundles.

This module is **pure**: it operates on bytes and key objects, never
touches the database, never reads the install's keystore, never
performs I/O. The keystore-resolution job belongs to
:mod:`matofx.migrate.service`; the DB-backed trust list belongs to
:mod:`matofx.migrate.trust`. Keeping signing.py free of side effects
makes it trivial to unit-test and reuse from the export and import
paths without coupling either to live infrastructure.

Same crypto stack as :mod:`matofx.audit.keys`
(``cryptography.hazmat.primitives.asymmetric.ed25519``) — we do not
introduce a second Ed25519 implementation.

A fingerprint here is the SHA-256 of the raw 32-byte Ed25519 public
key, rendered as a 64-character lowercase hex string with no
separators. Both length and case are part of the canonical form: any
mismatch on the wire is an error, not a soft mismatch the caller has
to remember to normalise. The UI / CLI render the colon-delimited
uppercase variant (``9F:2E:...``) via :func:`display_fingerprint`.

(Note: :mod:`matofx.audit.keys` truncates the same SHA-256 to 16 hex
chars for its fast-lookup ``key_id`` column. The migration trust
surface uses the **full** 64-char fingerprint because it is the
identifier a human eyeballs against another install's display string —
collisions in the truncated form would be catastrophic here.)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

FINGERPRINT_HEX_LEN: Final[int] = 64
"""SHA-256 in hex — 32 bytes, 64 characters."""

PUBLIC_KEY_HEX_LEN: Final[int] = 64
"""Raw Ed25519 public key in hex — 32 bytes, 64 characters."""

# Accepts anything the operator might paste: hex with or without colon
# / space / dash separators, with or without an ``SHA256:`` style prefix,
# uppercase or lowercase. The canonicaliser strips all of these and
# checks the surviving hex has the right length.
_FINGERPRINT_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:sha-?256|fingerprint|fp)\s*[:=]\s*", re.IGNORECASE
)
_FINGERPRINT_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[\s:\-_]+")
_HEX_ONLY_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]+$")


class BundleSigningError(Exception):
    """Raised on signing / verification / fingerprint failures.

    Message is plain English and aimed at the operator, not the
    developer — the same strings surface in the UI's import status
    log and in CLI stderr.
    """


@dataclass(frozen=True, slots=True)
class KeyPair:
    """Lightweight Ed25519 key pair holder used by tests and the service layer.

    The service layer normally derives this from
    :func:`matofx.audit.keys.load_signing_key` (or whatever per-install
    key loader lives there once the migrate keystore-resolution lands).
    Tests construct one directly via :meth:`generate`.
    """

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @classmethod
    def generate(cls) -> KeyPair:
        """Make a fresh in-memory key pair. Intended for tests only —
        production keys are persisted via :mod:`matofx.audit.keys`."""
        priv = Ed25519PrivateKey.generate()
        return cls(private_key=priv, public_key=priv.public_key())

    @classmethod
    def from_private_key(cls, private_key: Ed25519PrivateKey) -> KeyPair:
        return cls(private_key=private_key, public_key=private_key.public_key())


# ---------------------------------------------------------------------------
# Public-key encoding
# ---------------------------------------------------------------------------


def public_key_to_hex(public_key: Ed25519PublicKey) -> str:
    """Return the 64-char hex of the raw 32-byte Ed25519 public key.

    This is the encoding embedded in the bundle's ``manifest.json``.
    Compact, easy to eyeball, copies cleanly through email / chat /
    log forwarders that mangle base64.
    """
    raw = _public_key_raw_bytes(public_key)
    return raw.hex()


def public_key_from_hex(hex_str: str) -> Ed25519PublicKey:
    """Inverse of :func:`public_key_to_hex` with strict validation.

    Raises :class:`BundleSigningError` on any malformed input — the
    operator sees the message verbatim, so the wording matters.
    """
    cleaned = hex_str.strip().lower()
    if len(cleaned) != PUBLIC_KEY_HEX_LEN:
        raise BundleSigningError(
            f"The bundle's source public key is the wrong length "
            f"(expected {PUBLIC_KEY_HEX_LEN} hex characters, got "
            f"{len(cleaned)}). The bundle is corrupted or truncated."
        )
    if not _HEX_ONLY_RE.match(cleaned):
        raise BundleSigningError(
            "The bundle's source public key contains characters that are "
            "not hexadecimal. The bundle is corrupted or has been edited."
        )
    try:
        raw = bytes.fromhex(cleaned)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:  # pragma: no cover — defence in depth
        raise BundleSigningError(
            "The bundle's source public key could not be parsed as an "
            "Ed25519 public key. The bundle is corrupted."
        ) from exc


# ---------------------------------------------------------------------------
# Fingerprint computation, canonicalisation, display
# ---------------------------------------------------------------------------


def compute_fingerprint(public_key: Ed25519PublicKey) -> str:
    """Return the canonical fingerprint (lowercase hex, no separators).

    Fingerprint = SHA-256 of the 32-byte raw public key, hex-encoded.
    """
    raw = _public_key_raw_bytes(public_key)
    return hashlib.sha256(raw).hexdigest()


def canonicalise_fingerprint(pasted: str) -> str:
    """Normalise a user-pasted fingerprint to canonical form.

    Accepts every reasonable variant an operator might paste:

    * ``9F:2E:A1:7B:04:3D:8C:E1:...`` (colon-delimited, uppercase or lowercase)
    * ``9f2ea17b043d8ce1...`` (bare lowercase hex, what we store)
    * ``SHA256:9F:2E:...`` or ``sha-256: 9f 2e ...`` (prefixed)
    * ``fp: 9F-2E-A1-...`` (dash / underscore separators)

    Returns canonical lowercase hex, no separators. Raises
    :class:`BundleSigningError` with a plain-English message on any
    input that can't be coerced to a 64-character hex string.
    """
    if not pasted or not pasted.strip():
        raise BundleSigningError(
            "Fingerprint is empty. Paste the source install's fingerprint string into this field."
        )
    stripped = _FINGERPRINT_PREFIX_RE.sub("", pasted.strip(), count=1)
    no_seps = _FINGERPRINT_SEPARATOR_RE.sub("", stripped)
    lowered = no_seps.lower()
    if not _HEX_ONLY_RE.match(lowered):
        raise BundleSigningError(
            "Fingerprint contains characters that are not hexadecimal. "
            "A valid fingerprint uses only digits 0-9 and letters a-f, "
            "with optional colons, spaces, or dashes as separators."
        )
    if len(lowered) != FINGERPRINT_HEX_LEN:
        raise BundleSigningError(
            f"Fingerprint has the wrong length (expected "
            f"{FINGERPRINT_HEX_LEN} hex characters after removing "
            f"separators, got {len(lowered)}). Double-check that you "
            "copied the full fingerprint shown on the source install."
        )
    return lowered


def display_fingerprint(canonical: str) -> str:
    """Render the colon-delimited uppercase form for human display.

    ``"9f2ea17b04..."`` → ``"9F:2E:A1:7B:04:..."``. Input must already
    be canonical (lowercase hex, no separators); pass through
    :func:`canonicalise_fingerprint` first if it might not be.
    """
    if len(canonical) != FINGERPRINT_HEX_LEN or not _HEX_ONLY_RE.match(canonical):
        raise BundleSigningError(
            "Cannot format a non-canonical fingerprint for display. "
            "(This is an internal error — please report it.)"
        )
    upper = canonical.upper()
    return ":".join(upper[i : i + 2] for i in range(0, len(upper), 2))


def fingerprints_equal(a: str, b: str) -> bool:
    """Compare two fingerprints in canonical form. Constant-time."""
    if len(a) != len(b):
        return False
    diff = 0
    for ca, cb in zip(a, b, strict=True):
        diff |= ord(ca) ^ ord(cb)
    return diff == 0


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_manifest(manifest_bytes: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """Return the detached Ed25519 signature over ``manifest_bytes``.

    Pure: no I/O, no side effects.
    """
    if not isinstance(manifest_bytes, (bytes, bytearray)):
        raise TypeError(  # pragma: no cover — programmer error
            "manifest_bytes must be bytes"
        )
    return private_key.sign(bytes(manifest_bytes))


def verify_manifest(
    manifest_bytes: bytes,
    signature: bytes,
    public_key: Ed25519PublicKey,
) -> None:
    """Verify ``signature`` is a valid Ed25519 signature over ``manifest_bytes``.

    Returns ``None`` on success; raises :class:`BundleSigningError`
    with a plain-English message on failure. The same message surfaces
    verbatim in the import status log.
    """
    try:
        public_key.verify(bytes(signature), bytes(manifest_bytes))
    except InvalidSignature as exc:
        raise BundleSigningError(
            "The bundle's signature does not match its manifest. "
            "Either the bundle was modified after it was exported, or "
            "the signature was generated with a different key than the "
            "one embedded in the bundle. The bundle cannot be trusted; "
            "ask the source install to re-export."
        ) from exc


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _public_key_raw_bytes(public_key: Ed25519PublicKey) -> bytes:
    """Return the 32-byte raw form of the public key."""
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
