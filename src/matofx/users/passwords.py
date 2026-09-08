"""Argon2id password hashing.

Argon2id is the modern best-practice password hash (winner of the
Password Hashing Competition; OWASP-recommended for new applications).
Parameters are tunable via :class:`matofx.config.AuthSettings` so an
operator on slower hardware can keep the verify cost bounded.

Public surface:

- :class:`PasswordHasher` — wraps argon2-cffi with our settings; carries
  a precomputed dummy hash for timing-equalising "no such user" paths.
- :func:`validate_password` — runs the (Phase 1: minimal) password
  policy. Returns the failing reason, or ``None`` when the password
  is acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from argon2 import PasswordHasher as _Argon2Hasher
from argon2 import exceptions as _argon2_exc

if TYPE_CHECKING:
    from matofx.config import AuthSettings


# A constant string we hash at make_password_hasher time so the dummy
# verify in the unknown-username path runs against a plausible-looking
# Argon2 hash. The raw value never matters — what matters is that the
# verify takes the same wall time as a real one.
_DUMMY_PLAINTEXT = "__matofx_dummy_no_such_user__"


@dataclass(frozen=True)
class PasswordHasher:
    """Per-process password-hashing helper, parameterised from settings.

    Build once at startup with :func:`make_password_hasher`; pass into
    services that need to hash or verify passwords. The ``needs_rehash``
    method tells the caller when stored hashes are below the current
    cost target so they can re-hash on next successful login.

    ``dummy_hash`` is a real Argon2 hash computed at construction with
    the same parameters as the current install. Authentication code
    paths that don't find a user call ``verify(dummy_hash, attempted)``
    so the timing matches a real verify — defends against the timing
    channel that would otherwise leak whether a username exists.
    """

    _argon2: _Argon2Hasher
    dummy_hash: str
    """A precomputed Argon2 hash for the unknown-username timing-equaliser."""

    def hash(self, plaintext: str) -> str:
        """Return a fresh PHC-format Argon2id hash for ``plaintext``."""
        return self._argon2.hash(plaintext)

    def verify(self, hashed: str, plaintext: str) -> bool:
        """Constant-time verify; returns ``False`` on any mismatch.

        We translate every argon2-cffi exception (invalid hash, mismatch,
        etc.) into ``False`` because the caller never wants to behave
        differently across failure modes — the only safe response is the
        single :class:`matofx.users.errors.InvalidCredentialsError` outcome.
        """
        try:
            return self._argon2.verify(hashed, plaintext)
        except (
            _argon2_exc.VerifyMismatchError,
            _argon2_exc.VerificationError,
            _argon2_exc.InvalidHashError,
        ):
            return False

    def equalising_verify(self, hashed: str | None, plaintext: str) -> bool:
        """Verify ``plaintext`` against ``hashed`` with constant-time properties.

        When ``hashed`` is ``None`` (unknown user), this still runs an
        Argon2 verify against the precomputed :attr:`dummy_hash` so the
        wall time matches the known-user path. Returns ``False`` in
        that case. Use this on every authentication path that has a
        "user not found" branch.
        """
        if hashed is None:
            self.verify(self.dummy_hash, plaintext)
            return False
        return self.verify(hashed, plaintext)

    def needs_rehash(self, hashed: str) -> bool:
        """``True`` when stored hash uses weaker parameters than the current target."""
        return self._argon2.check_needs_rehash(hashed)


def make_password_hasher(settings: AuthSettings) -> PasswordHasher:
    """Build a :class:`PasswordHasher` from validated auth settings."""
    argon2 = _Argon2Hasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
    )
    return PasswordHasher(_argon2=argon2, dummy_hash=argon2.hash(_DUMMY_PLAINTEXT))


def validate_password(plaintext: str, settings: AuthSettings) -> str | None:
    """Run baseline password policy. Return failure reason or ``None``.

    Checks length only. The character-class rules (upper / lower /
    digit / symbol) live in :func:`validate_password_complexity` and
    apply at the API boundary where a user submits a password —
    keeping them out of this function avoids breaking unit tests
    that exercise the data layer directly with simple fixtures.
    """
    if len(plaintext) < settings.password_min_length:
        return (
            f"Password is too short. Please use at least {settings.password_min_length} characters."
        )
    return None


def validate_password_complexity(plaintext: str, snapshot: dict) -> str | None:
    """Run length + character-class policy against the General Settings snapshot.

    ``snapshot`` is the dict returned by
    :func:`matofx.config.load_general_settings`. The relevant keys:

    * ``password_min_length`` — int. Passwords shorter than this are
      rejected first (covers what :func:`validate_password` checks
      against the TOML baseline; including it here means an admin's
      DB save takes effect at the API boundary without restart).
    * ``password_require_uppercase`` / ``_lowercase`` / ``_digit`` /
      ``_symbol`` — bool toggles. When true, the password must
      contain at least one character of that class.
    * ``password_allowed_symbols`` — the full set of symbol
      characters permitted. The password's alphabet is restricted
      to ``a-zA-Z0-9`` plus these characters; anything outside the
      union is rejected.

    Returns a plain-English failure reason, or ``None`` when the
    password satisfies every enabled rule.

    Call this at the boundary where a user submits a password
    (sign-up, change-password, admin reset). The data-layer
    :class:`matofx.users.UserService` deliberately doesn't call it
    so test fixtures can keep using simple passwords without the
    site-wide policy interfering.
    """
    min_length_raw = snapshot.get("password_min_length")
    if isinstance(min_length_raw, int) and not isinstance(min_length_raw, bool):
        if len(plaintext) < min_length_raw:
            return f"Password is too short. Please use at least {min_length_raw} characters."
    allowed_symbols = str(snapshot.get("password_allowed_symbols", ""))
    allowed_alphabet = set(allowed_symbols)
    has_upper = False
    has_lower = False
    has_digit = False
    has_symbol = False
    for ch in plaintext:
        if "A" <= ch <= "Z":
            has_upper = True
        elif "a" <= ch <= "z":
            has_lower = True
        elif "0" <= ch <= "9":
            has_digit = True
        elif ch in allowed_alphabet:
            has_symbol = True
        else:
            return (
                f"Password contains a character that isn't allowed ({ch!r}). "
                "Allowed: letters, digits, and "
                f"{allowed_symbols!r}."
            )

    if bool(snapshot.get("password_require_uppercase", False)) and not has_upper:
        return "Password must contain at least one uppercase letter (A-Z)."
    if bool(snapshot.get("password_require_lowercase", False)) and not has_lower:
        return "Password must contain at least one lowercase letter (a-z)."
    if bool(snapshot.get("password_require_digit", False)) and not has_digit:
        return "Password must contain at least one digit (0-9)."
    if bool(snapshot.get("password_require_symbol", False)) and not has_symbol:
        return (
            f"Password must contain at least one symbol from the allowed set: {allowed_symbols!r}."
        )
    return None
