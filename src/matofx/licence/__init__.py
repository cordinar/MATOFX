"""Licence subsystem — the single authority on what this install may do.

Per brief §C16, no business logic checks the licence directly: the auth
layer asks this module whether a session slot is available, the admin UI
asks whether a floating assignment can be granted, and the read-only
middleware asks whether the site is still writable. Everything routes
through :func:`resolve_licence`.

Two licence types, one active at a time (declared by the manifest):

- **CSL** — N concurrent sessions; a slot is consumed at login.
- **FAL** — N access-rights assignments; a slot is consumed when an
  admin grants the floating right.

Enforcement lands in slices 4-6. This slice supplies the manifest, its
verification, its state (`active → grace → expired`), and the bridge to
the compliance report.
"""

from __future__ import annotations

from matofx.licence.assignments import (
    AssignmentDecision,
    admit_assignment,
    count_assignments,
    release_assignment,
)
from matofx.licence.errors import (
    LicenceAssignmentsExhaustedError,
    LicenceError,
    LicenceInvalidError,
    LicenceMissingError,
)
from matofx.licence.features import apply_licence_features, effective_feature_settings
from matofx.licence.loader import LoadedLicence, issuer_required, load_licence_file
from matofx.licence.machine import machine_fingerprint
from matofx.licence.models import (
    MANIFEST_SCHEMA_VERSION,
    LicenceManifest,
    LicenceState,
    LicenceStatus,
    LicenceType,
    parse_manifest,
)
from matofx.licence.orm import LicenceInstall
from matofx.licence.service import (
    LicenceResolution,
    current_install,
    install_licence,
    licence_snapshot,
    record_validation,
    report_identity,
    resolve_licence,
    verify_install_binding,
)
from matofx.licence.startup import check_licence_at_startup, recheck_licence

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "AssignmentDecision",
    "LicenceAssignmentsExhaustedError",
    "LicenceError",
    "LicenceInstall",
    "LicenceInvalidError",
    "LicenceManifest",
    "LicenceMissingError",
    "LicenceResolution",
    "LicenceState",
    "LicenceStatus",
    "LicenceType",
    "LoadedLicence",
    "admit_assignment",
    "apply_licence_features",
    "check_licence_at_startup",
    "count_assignments",
    "current_install",
    "effective_feature_settings",
    "install_licence",
    "issuer_required",
    "licence_snapshot",
    "load_licence_file",
    "machine_fingerprint",
    "parse_manifest",
    "recheck_licence",
    "record_validation",
    "release_assignment",
    "report_identity",
    "resolve_licence",
    "verify_install_binding",
]
