"""Secrets store — opaque ``secret_ref`` → secret material resolution.

Several places in MATOFX hold a ``secret_ref`` column on a row: AOS
hosts, HPC clusters, future external integrations. The ``secret_ref``
is intentionally opaque to the database — the actual credential lives
in a secrets backend (HashiCorp Vault, AWS Secrets Manager, Kubernetes
Secret, OS keyring, etc.), and the store resolves the ref into the
material at point of use.

Public surface:

- :class:`SecretsStore` — async :class:`typing.Protocol` every backend
  implements. The runner / API construct one at startup and pass it
  into the targets / services that need it.
- :class:`Secret` — a frozen wrapper around the material. Bytes-typed
  so it works equally well for passwords and private-key files. Has a
  redacted :func:`repr` so a secret accidentally logged at the wrong
  level is still safe.
- :class:`InMemorySecretsStore` — a tests / dev backend. Real
  deployments plug their own backend in via the same protocol.
- :class:`SecretsError`, :class:`SecretNotFoundError` — plain-English
  errors for surfacing into operator UIs.
"""

from __future__ import annotations

from matofx.secrets.encrypted_file import (
    EncryptedFileSecretsStore,
    build_fernet,
    generate_master_key,
    make_secrets_store,
    open_encrypted_store,
)
from matofx.secrets.errors import (
    SecretNotFoundError,
    SecretsConfigError,
    SecretsDecryptError,
    SecretsError,
)
from matofx.secrets.store import InMemorySecretsStore, Secret, SecretsStore

__all__ = [
    "EncryptedFileSecretsStore",
    "InMemorySecretsStore",
    "Secret",
    "SecretNotFoundError",
    "SecretsConfigError",
    "SecretsDecryptError",
    "SecretsError",
    "SecretsStore",
    "build_fernet",
    "generate_master_key",
    "make_secrets_store",
    "open_encrypted_store",
]
