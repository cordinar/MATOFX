"""Compliance attestation — signed, anchored licence reports (ADR-0019).

Funis asks a customer for a report; the customer's admin generates one
offline and sends the artefact back; Funis checks it against a ledger of
heads it already holds. Nothing phones home (§B10).

The evidential weight is in the **anchoring**, not the signature: a
report cites the previous report's chain head *as it hashes right now*,
and Funis compares that against what it recorded last time. An install
that rewrote its history cannot satisfy a value it has no way to read.

Both licence types are first-class throughout — CSL measures peak
concurrency, FAL measures assignments held, and the report carries a
discriminated block for each with the authoritative one named.
"""

from __future__ import annotations

from matofx.compliance.errors import (
    ComplianceError,
    InstallIdentityError,
    LedgerError,
    ReportFormatError,
    ReportSigningError,
)
from matofx.compliance.identity import (
    IssuedIdentity,
    adopt_identity,
    current_identity,
    load_identity_file,
    next_report_counter,
)
from matofx.compliance.ledger import (
    LEDGER_GENESIS_HASH,
    HeadMarker,
    LedgerEntry,
    LedgerSigner,
    LedgerVerification,
    append_entry,
    entry_from_report,
    known_public_key,
    latest_for,
    read_head_marker,
    read_ledger,
    verify_ledger,
    write_head_marker,
)
from matofx.compliance.orm import ComplianceReport, InstallIdentity
from matofx.compliance.render import render_report_html
from matofx.compliance.report import GeneratedReport, LicenceSnapshot, generate_report
from matofx.compliance.schema import (
    CSL,
    FAL,
    LICENCE_EVENT_ACTIONS,
    REPORT_SCHEMA_VERSION,
    CslUsage,
    FalUsage,
    LicenceType,
    UsageBlock,
)
from matofx.compliance.verify import (
    CONSISTENT,
    CONTRADICTED,
    INCONCLUSIVE,
    Check,
    Finding,
    ReportVerdict,
    verify_report,
)

__all__ = [
    "CONSISTENT",
    "CONTRADICTED",
    "CSL",
    "FAL",
    "INCONCLUSIVE",
    "LEDGER_GENESIS_HASH",
    "LICENCE_EVENT_ACTIONS",
    "REPORT_SCHEMA_VERSION",
    "Check",
    "ComplianceError",
    "ComplianceReport",
    "CslUsage",
    "FalUsage",
    "Finding",
    "GeneratedReport",
    "HeadMarker",
    "InstallIdentity",
    "InstallIdentityError",
    "IssuedIdentity",
    "LedgerEntry",
    "LedgerError",
    "LedgerSigner",
    "LedgerVerification",
    "LicenceSnapshot",
    "LicenceType",
    "ReportFormatError",
    "ReportSigningError",
    "ReportVerdict",
    "UsageBlock",
    "adopt_identity",
    "append_entry",
    "current_identity",
    "entry_from_report",
    "generate_report",
    "known_public_key",
    "latest_for",
    "load_identity_file",
    "next_report_counter",
    "read_head_marker",
    "read_ledger",
    "render_report_html",
    "verify_ledger",
    "verify_report",
    "write_head_marker",
]
