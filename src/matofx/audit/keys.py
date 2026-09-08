"""Ed25519 signing key handling.

Audit records are signed with an Ed25519 private key. The public half
identifies the key via :func:`compute_key_id` — a short hex prefix of
the SHA-256 of the raw public bytes. That same key id is stored on every
record so a verifier can match records to keys after a key rotation.

Keys are stored on disk as PEM (PKCS#8 unencrypted). Future v1.x work
adds passphrase-protected keys and TPM-backed keys per §C16; the
:func:`load_signing_key` API will gain optional parameters then without
changing call sites that don't need them.
"""

from __future__ import annotations

import contextlib
import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_ID_HEX_LEN: Final[int] = 16
"""Length in hex characters of the truncated key id (8 bytes)."""


class AuditKeyError(Exception):
    """Raised on key loading / generation failures.

    Message is plain English with actionable guidance — the operator
    sees this in startup logs and the ``matofx doctor`` output.
    """


def generate_signing_key(path: Path) -> Ed25519PrivateKey:
    """Generate a fresh Ed25519 key and write it to ``path`` as PEM.

    Refuses to overwrite an existing file. Sets owner-only file
    permissions on POSIX (best-effort on Windows).
    """
    if path.exists():
        raise AuditKeyError(
            f"Refusing to overwrite existing signing key at {path}. "
            "Move or remove the file first if you really mean to rotate."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    # Best-effort tightening of file perms; Windows / non-POSIX FS may not honour it.
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return key


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    """Load a PEM-encoded Ed25519 private key from ``path``.

    Emits a ``warnings.warn`` on POSIX hosts when the file is readable
    by anyone other than the owner — this is a private key and should
    be ``chmod 600``. Suppress with ``MATOFX_SUPPRESS_PERM_WARNING=1``
    on shared dev hosts.
    """
    if not path.is_file():
        raise AuditKeyError(
            f"Signing key not found at {path}. "
            "Generate one with `matofx rotate signing-key --init` "
            "or update audit.signing_key_path in matofx.toml."
        )
    _maybe_warn_key_permissions(path)
    pem = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise AuditKeyError(f"Could not parse signing key at {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AuditKeyError(
            f"Key at {path} is not an Ed25519 private key "
            f"(got {type(key).__name__}). Generate a new one with "
            "`matofx rotate signing-key --init`."
        )
    return key


_PERM_SUPPRESS_ENV = "MATOFX_SUPPRESS_PERM_WARNING"


def _maybe_warn_key_permissions(path: Path) -> None:
    """Warn if the signing-key file is readable by others on POSIX."""
    import os  # noqa: PLC0415 — kept local
    import sys  # noqa: PLC0415

    if sys.platform == "win32":
        return
    if os.environ.get(_PERM_SUPPRESS_ENV):
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        warnings.warn(
            (
                f"Audit signing key {path} is readable by other users "
                f"(mode {oct(mode & 0o777)}). This is a private key — "
                f"tighten with `chmod 600` or set {_PERM_SUPPRESS_ENV}=1 "
                "to silence this warning (e.g. on shared dev hosts)."
            ),
            stacklevel=3,
        )


def compute_key_id(public_key: Ed25519PublicKey) -> str:
    """Stable, short identifier for a public key.

    SHA-256 of the raw 32-byte public key, truncated to
    :data:`KEY_ID_HEX_LEN` hex characters. Long enough to be unique in
    practice for the small key inventory a single install carries.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:KEY_ID_HEX_LEN]


# ---------------------------------------------------------------------------
# Key rotation with chain continuity
# ---------------------------------------------------------------------------
#
# Audit records store the ``key_id`` of the key that signed them, so a chain
# can outlive any single key: records signed by a retired key stay verifiable
# as long as the verifier still has that key's *public* half. Rotation
# therefore retires the outgoing public key into a sibling directory before
# minting the new one, and :func:`collect_public_keys` gathers the active key
# plus every retired public key so ``matofx audit verify`` covers the whole
# chain. See :func:`rotate_signing_key`.


def retired_keys_dir(signing_key_path: Path) -> Path:
    """Directory holding retired public keys for ``signing_key_path``.

    A sibling of the active key file so it travels with the key in backups.
    """
    return signing_key_path.parent / (signing_key_path.name + ".retired")


def _public_key_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def collect_public_keys(signing_key_path: Path) -> dict[str, Ed25519PublicKey]:
    """Return ``{key_id: public_key}`` for the active key plus all retired keys.

    The active key file may be absent (returns just the retired set); an
    unreadable or non-Ed25519 retired file is skipped rather than aborting
    the whole verification. Used by ``matofx audit verify`` so records signed
    before a rotation remain verifiable.
    """
    keys: dict[str, Ed25519PublicKey] = {}
    if signing_key_path.is_file():
        active = load_signing_key(signing_key_path).public_key()
        keys[compute_key_id(active)] = active
    retired = retired_keys_dir(signing_key_path)
    if retired.is_dir():
        for pub_file in sorted(retired.glob("*.pub")):
            try:
                loaded = serialization.load_pem_public_key(pub_file.read_bytes())
            except (ValueError, TypeError):
                continue
            if isinstance(loaded, Ed25519PublicKey):
                keys[compute_key_id(loaded)] = loaded
    return keys


@dataclass(frozen=True)
class RotationResult:
    """Outcome of :func:`rotate_signing_key`."""

    new_key_id: str
    old_key_id: str | None
    """``None`` on ``--init`` (there was no prior key)."""
    retired_public_key_path: Path | None
    archived_private_key_path: Path | None


def rotate_signing_key(path: Path, *, init: bool) -> RotationResult:
    """Initialise or rotate the audit signing key at ``path``.

    With ``init=True`` this mints the very first key and refuses if one
    already exists. Without it, an existing key is *rotated*: its public half
    is retired into :func:`retired_keys_dir` (so historical records stay
    verifiable), its private half is archived alongside for disaster
    recovery, and a fresh key takes its place. Either way the caller must
    restart the server for the new key to take effect.
    """
    exists = path.is_file()
    if init:
        if exists:
            raise AuditKeyError(
                f"--init refused: a signing key already exists at {path}. "
                "Run `matofx rotate signing-key` (without --init) to rotate it."
            )
        key = generate_signing_key(path)
        return RotationResult(
            new_key_id=compute_key_id(key.public_key()),
            old_key_id=None,
            retired_public_key_path=None,
            archived_private_key_path=None,
        )

    if not exists:
        raise AuditKeyError(
            f"No signing key at {path} to rotate. "
            "Run `matofx rotate signing-key --init` to create the first key."
        )

    old_key = load_signing_key(path)
    old_id = compute_key_id(old_key.public_key())

    retired = retired_keys_dir(path)
    retired.mkdir(parents=True, exist_ok=True)
    retired_pub = retired / f"{old_id}.pub"
    retired_pub.write_bytes(_public_key_pem(old_key.public_key()))

    # Archive the old private key beside its retired public key (recovery /
    # forensic use) then clear the path so generate_signing_key's
    # no-overwrite guard is satisfied.
    #
    # SECURITY: this .key.bak is a *private* signing key that still verifies
    # as authentic for historical key_ids, so read access to the retired dir
    # is equivalent to holding a valid signing key. The chmod(0o600) below is
    # best-effort and a NO-OP on Windows (the documented deploy target), where
    # the file inherits the directory ACL. The signing-key directory — the
    # active key AND this .retired/ archive — MUST be ACL-locked to the
    # service account on Windows deployments (see the admin "Audit trail"
    # docs). Do not place it on a broadly-readable share.
    archived_priv = retired / f"{old_id}.key.bak"
    archived_priv.write_bytes(path.read_bytes())
    with contextlib.suppress(OSError):
        archived_priv.chmod(0o600)
    path.unlink()

    new_key = generate_signing_key(path)
    return RotationResult(
        new_key_id=compute_key_id(new_key.public_key()),
        old_key_id=old_id,
        retired_public_key_path=retired_pub,
        archived_private_key_path=archived_priv,
    )
