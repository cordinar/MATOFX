"""Cryptographically-secure session-token generation and hashing.

The plaintext session token is what the client holds (cookie, header,
whatever the UI layer chooses). It is never stored — only its SHA-256
hash lives in the ``auth_session`` row. A DB compromise therefore
cannot be used to hijack live sessions.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

_TOKEN_BYTES: Final[int] = 32


def new_session_token() -> str:
    """Return a fresh 256-bit URL-safe session token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """SHA-256 of the token bytes, hex-encoded."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
