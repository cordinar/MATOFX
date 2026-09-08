"""Secrets store protocol + an in-memory implementation for tests / dev.

A :class:`SecretsStore` is an async, opaque ``str → bytes`` lookup.
The protocol is small on purpose — every real backend (Vault, AWS
Secrets Manager, OS keyring, etc.) reduces to "given a reference,
return the bytes of the secret". The MATOFX modules that consume
secrets (AOS targets, HPC targets, future integrations) hold the
:class:`SecretsStore` protocol type, never a concrete backend, so
swapping the backend is a one-line change at process startup.

Phase 1 ships only the in-memory backend so tests and dev
deployments can run without a real secrets system. Phase 2 will
add a Vault backend (and possibly an OS-keyring one for single-node
installs) under the same protocol.

Why bytes (not ``str``)?

- SSH private-key files include trailing newlines and may be PEM or
  OpenSSH-format; ``bytes`` is what asyncssh expects and what
  ``Path.read_bytes()`` produces.
- Passwords are always representable as UTF-8 bytes; consumers that
  want a string call :meth:`Secret.as_str` (which decodes once).
- Treating everything uniformly avoids the encoding-mismatch bugs
  that come with a mixed-type API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from matofx.secrets.errors import SecretNotFoundError


@dataclass(frozen=True)
class Secret:
    """A frozen wrapper around secret material.

    Carries the bytes plus the originating reference so the consumer
    can include the ref (which is non-secret) in audit / log lines
    without leaking the material. The ``repr`` deliberately redacts
    the bytes so an accidental ``log.debug(secret)`` doesn't leak
    the credential.
    """

    secret_ref: str
    """The opaque reference the store was asked to resolve."""

    material: bytes
    """The secret bytes. Treat as opaque; never log directly."""

    def __repr__(self) -> str:
        """Redacted repr — never echo the material."""
        return (
            f"Secret(secret_ref={self.secret_ref!r}, "
            f"material=<{len(self.material)} bytes redacted>)"
        )

    def as_bytes(self) -> bytes:
        """Return the raw material as bytes."""
        return self.material

    def as_str(self, encoding: str = "utf-8") -> str:
        """Return the material decoded as a string.

        Args:
            encoding: Codec to decode with. Defaults to UTF-8 which
                covers passwords and ASCII-only key files; pass an
                explicit alternate codec for non-UTF-8 secrets.
        """
        return self.material.decode(encoding)


class SecretsStore(Protocol):
    """Async lookup from opaque ``secret_ref`` to :class:`Secret`."""

    async def resolve(self, secret_ref: str) -> Secret:
        """Return the secret stored at ``secret_ref``.

        Raises:
            SecretNotFoundError: The ref isn't known to this store.
        """
        ...


class InMemorySecretsStore:
    """A SecretsStore backed by a Python dict — for tests and dev only.

    Real deployments use a Vault / AWS Secrets Manager / OS-keyring
    backend that implements the same :class:`SecretsStore` protocol.
    This implementation deliberately offers no persistence, no
    rotation, and no audit — calling it suitable for production would
    be a category error.
    """

    def __init__(self, initial: dict[str, bytes] | None = None) -> None:
        """Construct, optionally seeded with ``{secret_ref: bytes}``."""
        self._secrets: dict[str, bytes] = dict(initial) if initial else {}

    def put(self, secret_ref: str, material: bytes | str) -> None:
        """Add or replace a secret. Strings are UTF-8 encoded."""
        self._secrets[secret_ref] = (
            material if isinstance(material, bytes) else material.encode("utf-8")
        )

    def remove(self, secret_ref: str) -> None:
        """Remove a secret if present; missing refs are a no-op."""
        self._secrets.pop(secret_ref, None)

    async def resolve(self, secret_ref: str) -> Secret:
        """Return the stored secret or raise :class:`SecretNotFoundError`."""
        material = self._secrets.get(secret_ref)
        if material is None:
            raise SecretNotFoundError(secret_ref)
        return Secret(secret_ref=secret_ref, material=material)
