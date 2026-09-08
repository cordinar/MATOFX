"""Encrypted-file :class:`SecretsStore` — the production single-node backend.

Secrets live in one JSON file mapping each opaque ``secret_ref`` to a
Fernet token (authenticated AES-128-CBC + HMAC). The file at rest reveals
only the *ref names* (which are non-secret by design — they're stored in
plaintext ``secret_ref`` columns in the DB already), never the material.

The master key is a Fernet key supplied through ``[secrets].master_key``
in ``matofx.toml`` (or the ``MATOFX__SECRETS__MASTER_KEY`` env var),
generated once with ``matofx secrets init``. Wrong key or a tampered file
fails closed (:class:`SecretsDecryptError`) rather than guessing.

Why a file (not Vault / a cloud secrets manager)? MATOFX targets the
lightweight single-node install; a dependency-free encrypted file keeps
the deployment small. Larger sites can implement the same
:class:`~matofx.secrets.SecretsStore` protocol against their own backend
and swap it in at the one construction site (``make_secrets_store``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from matofx.secrets.errors import (
    SecretNotFoundError,
    SecretsConfigError,
    SecretsDecryptError,
)
from matofx.secrets.store import Secret, SecretsStore

if TYPE_CHECKING:
    from matofx.config.models import SecretsSettings


def generate_master_key() -> str:
    """Return a fresh urlsafe-base64 Fernet key as a string.

    Backs ``matofx secrets init``: the operator pastes the output into
    ``[secrets].master_key`` (or the matching env var). 32 random bytes,
    base64-encoded — never derived from a passphrase, so there's no
    weak-password failure mode.
    """
    return Fernet.generate_key().decode("ascii")


def build_fernet(master_key: str) -> Fernet:
    """Build a :class:`Fernet` from the configured master key.

    Raises :class:`SecretsConfigError` (not a raw ``ValueError``) when the
    key isn't a valid Fernet key, so the operator gets an actionable
    message pointing at the config rather than a stack trace.
    """
    try:
        return Fernet(master_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretsConfigError(
            "[secrets].master_key is not a valid key. Generate one with "
            "`matofx secrets init` and paste it into matofx.toml (or set "
            "MATOFX__SECRETS__MASTER_KEY)."
        ) from exc


class EncryptedFileSecretsStore:
    """A :class:`~matofx.secrets.SecretsStore` backed by one Fernet-encrypted file.

    Read path (:meth:`resolve`) is async per the protocol — file IO runs
    off the event loop. Write paths (:meth:`set_secret` / :meth:`remove_secret`
    / :meth:`list_refs`) are synchronous because the only caller is the
    ``matofx secrets`` CLI, which is a short-lived process.
    """

    def __init__(self, file_path: Path, fernet: Fernet) -> None:
        """Construct over ``file_path`` using a prepared ``fernet``."""
        self._path = Path(file_path)
        self._fernet = fernet

    # ----- read path (protocol) ----------------------------------------

    async def resolve(self, secret_ref: str) -> Secret:
        """Return the secret stored at ``secret_ref``.

        Raises:
            SecretNotFoundError: the ref isn't in the file.
            SecretsDecryptError: wrong master key or the file was tampered.
        """
        return await asyncio.to_thread(self._resolve_sync, secret_ref)

    def _resolve_sync(self, secret_ref: str) -> Secret:
        tokens = self._read_tokens()
        token = tokens.get(secret_ref)
        if token is None:
            raise SecretNotFoundError(secret_ref)
        try:
            material = self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise SecretsDecryptError(secret_ref) from exc
        return Secret(secret_ref=secret_ref, material=material)

    # ----- write path (CLI) --------------------------------------------

    def set_secret(self, secret_ref: str, material: bytes | str) -> None:
        """Encrypt and store ``material`` under ``secret_ref`` (add or replace)."""
        raw = material if isinstance(material, bytes) else material.encode("utf-8")
        tokens = self._read_tokens()
        tokens[secret_ref] = self._fernet.encrypt(raw).decode("ascii")
        self._write_tokens(tokens)

    def remove_secret(self, secret_ref: str) -> bool:
        """Remove ``secret_ref`` if present. Returns whether anything was removed."""
        tokens = self._read_tokens()
        if secret_ref not in tokens:
            return False
        del tokens[secret_ref]
        self._write_tokens(tokens)
        return True

    def list_refs(self) -> list[str]:
        """Return the stored refs (non-secret), sorted."""
        return sorted(self._read_tokens().keys())

    def has_secret(self, secret_ref: str) -> bool:
        """Return whether ``secret_ref`` is present (without decrypting it)."""
        return secret_ref in self._read_tokens()

    # ----- file IO ------------------------------------------------------

    def _read_tokens(self) -> dict[str, str]:
        """Load the ``{ref: token}`` map; missing file ⇒ empty map."""
        if not self._path.is_file():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretsConfigError(
                f"The secrets file {self._path} could not be read as JSON. "
                "It may be corrupted; restore it from backup or re-create it."
            ) from exc
        if not isinstance(payload, dict):
            raise SecretsConfigError(
                f"The secrets file {self._path} is not a JSON object of "
                "reference → token entries."
            )
        # Coerce defensively: every value must be a string token.
        return {str(k): str(v) for k, v in payload.items()}

    def _write_tokens(self, tokens: dict[str, str]) -> None:
        """Atomically write the ``{ref: token}`` map to disk (0600 where supported).

        Writes to a temp file in the same directory then ``os.replace`` so a
        crash mid-write can't truncate the live store. Best-effort ``chmod``
        to owner-only — a no-op on Windows, which uses ACL inheritance.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(tokens, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".secrets-", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            # Best-effort owner-only perms; a no-op on Windows (ACL-based).
            with contextlib.suppress(OSError):
                tmp_path.chmod(0o600)
            tmp_path.replace(self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise


def make_secrets_store(settings: SecretsSettings) -> SecretsStore:
    """Build the configured :class:`~matofx.secrets.SecretsStore`.

    Returns an :class:`~matofx.secrets.InMemorySecretsStore` for the
    ``memory`` backend (dev / test / local-only installs) or an
    :class:`EncryptedFileSecretsStore` for ``encrypted_file``. Raises
    :class:`SecretsConfigError` if the encrypted-file backend is selected
    without a ``file_path`` / ``master_key``.
    """
    from matofx.secrets.store import InMemorySecretsStore  # noqa: PLC0415

    if settings.backend == "memory":
        return InMemorySecretsStore()
    return open_encrypted_store(settings)


def open_encrypted_store(settings: SecretsSettings) -> EncryptedFileSecretsStore:
    """Build the concrete :class:`EncryptedFileSecretsStore` from settings.

    Used by ``make_secrets_store`` and directly by the ``matofx secrets``
    CLI (which needs the concrete write methods). Raises
    :class:`SecretsConfigError` when ``file_path`` / ``master_key`` are
    absent.
    """
    if settings.file_path is None or settings.master_key is None:
        raise SecretsConfigError(
            "[secrets].backend is 'encrypted_file' but file_path and/or "
            "master_key are not set. Set both in matofx.toml (master_key from "
            "`matofx secrets init`)."
        )
    fernet = build_fernet(settings.master_key.get_secret_value())
    return EncryptedFileSecretsStore(settings.file_path, fernet)


__all__ = [
    "EncryptedFileSecretsStore",
    "build_fernet",
    "generate_master_key",
    "make_secrets_store",
    "open_encrypted_store",
]
