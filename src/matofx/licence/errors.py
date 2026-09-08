"""Errors raised by the licence subsystem (ADR-0019 §C, brief §C16).

The split between these two classes is the enforcement decision from
ADR-0019 §C11, and it matters:

- :class:`LicenceInvalidError` — the licence material is *wrong*: bad
  signature, malformed manifest, wrong machine. Someone edited it. The
  install **fails closed**.
- :class:`LicenceMissingError` — there is simply no licence installed.
  That is an administrative lapse, not tampering, so it degrades to
  read-only rather than refusing to serve.

Expiry is neither: an expired licence parses and verifies perfectly, so
it is a *state* (:mod:`matofx.licence.models`), never an exception.
§C16 is explicit that a lapsed renewal must never take a customer's
simulation org offline.
"""

from __future__ import annotations


class LicenceError(Exception):
    """Base class for every licence-subsystem failure."""


class LicenceInvalidError(LicenceError):
    """The licence material is malformed, untrusted, or bound elsewhere.

    Fail-closed: a deliberately edited licence is not an availability
    problem, and continuing would mean honouring terms nobody granted.
    """


class LicenceMissingError(LicenceError):
    """No licence file is configured, or the configured file is absent."""


class LicenceSlotsExhaustedError(LicenceError):
    """Every concurrent-session place is in use (§C16, CSL).

    Not an authentication failure — the credentials were correct — so it
    must not be reported as one. The message names the cap and says what
    the user can do about it, because "403 Forbidden" in front of a
    perfectly valid password is the single most confusing thing this
    subsystem could do to an engineer at 9am.
    """

    def __init__(self, message: str) -> None:
        """Construct from the plain-English refusal text."""
        super().__init__(message)


class LicenceAssignmentsExhaustedError(LicenceError):
    """Every floating-access assignment is taken (§C16, FAL).

    Raised when an administrator tries to give a further account access.
    A *client* error, not a capacity blip like the CSL equivalent — the
    admin has to take a decision (free one, or extend the licence), so
    it must not look like something that will fix itself if they retry.
    """

    def __init__(self, message: str) -> None:
        """Construct from the plain-English refusal text."""
        super().__init__(message)
