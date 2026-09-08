"""Errors raised by the secrets store; safe for operator UIs."""

from __future__ import annotations


class SecretsError(Exception):
    """Base class for secrets-store errors."""


class SecretNotFoundError(SecretsError):
    """The supplied ``secret_ref`` doesn't resolve to a stored secret.

    Surfaces when an AOS/HPC connection attempt resolves its
    credentials and the backend doesn't know about the ref.
    Operators see this when they've registered a host without
    seeding the corresponding secret in their secrets backend.
    """

    def __init__(self, secret_ref: str) -> None:
        """Construct with the offending ref (safe to surface — opaque by design)."""
        super().__init__(
            f"No secret was found for reference {secret_ref!r}. "
            "Add it to your secrets backend before retrying."
        )
        self.secret_ref = secret_ref


class SecretsConfigError(SecretsError):
    """The secrets backend is mis-configured and can't be constructed.

    Raised when ``[secrets].backend = "encrypted_file"`` is selected but
    the ``file_path`` / ``master_key`` are missing, or the master key
    isn't a valid Fernet key. Plain-English so operators can fix the
    ``matofx.toml`` and retry.
    """


class SecretsDecryptError(SecretsError):
    """A stored secret could not be decrypted — wrong key or tampered file.

    Fernet authenticates every token, so this fires when the master key
    doesn't match the one the secret was written with, or the store file
    was modified / corrupted. Fail-closed: the credential is treated as
    unavailable rather than guessed at.
    """

    def __init__(self, secret_ref: str) -> None:
        """Construct with the offending ref (opaque by design — safe to surface)."""
        super().__init__(
            f"The secret for reference {secret_ref!r} could not be decrypted. "
            "The master key may be wrong, or the secrets file may have been "
            "modified or corrupted. Check [secrets].master_key, or re-add the secret."
        )
        self.secret_ref = secret_ref
