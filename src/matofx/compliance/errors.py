"""Errors raised by the compliance-attestation subsystem (ADR-0019).

Messages are plain English and actionable — an operator sees them in
``matofx compliance …`` output and (from slice 1's admin surface) in the
dashboard, so they must say what to do, not just what went wrong.
"""

from __future__ import annotations


class ComplianceError(Exception):
    """Base class for every compliance-subsystem failure."""


class InstallIdentityError(ComplianceError):
    """The install-identity material is missing, malformed, or untrusted.

    Covers: no identity file configured, unreadable / non-JSON file,
    missing fields, a bad issuer signature, and an issuer key id the
    build doesn't trust. All of these mean "this install cannot produce
    a report Funis would believe", so they fail loudly rather than
    degrading to an unsigned artefact.
    """


class ReportSigningError(ComplianceError):
    """A report could not be signed (bad key material)."""


class ReportFormatError(ComplianceError):
    """A report file could not be parsed, or declares an unknown schema version."""


class LedgerError(ComplianceError):
    """The vendor-side head ledger is unreadable or malformed."""
