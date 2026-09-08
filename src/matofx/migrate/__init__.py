"""Cross-install migration of admin-released tools and workflows.

The feature lets a super-user (or an administrator who has been
granted :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS`) export a signed
tarball of one or more tool definitions and admin-released workflow
definitions from a source install (typically Staging), transfer it
manually to a destination install (typically Production), and import
it there with a plain-English live status log.

Scope, transport, trust, atomicity, and reference-resolution rules
are pinned in PROJECT_BRIEF.md §Q5 (2026-05-15 amendment). End-user
walkthrough is MIGRATE_GUIDE.md.

Module layout:

- :mod:`matofx.migrate.orm` — :class:`MigrateTrustedPeer` table.
- :mod:`matofx.migrate.bundle` — canonical tarball pack/unpack +
  ``manifest.json`` schema. (Pending.)
- :mod:`matofx.migrate.signing` — Ed25519 sign/verify against the
  trust store; ``canonicalise_fingerprint`` helper. (Pending.)
- :mod:`matofx.migrate.trust` — :class:`TrustedPeerService` for
  add/remove/list, fully audited. (Pending.)
- :mod:`matofx.migrate.service` — :class:`ExportService` /
  :class:`ImportService`; per-item atomicity, plain-English
  outcomes. (Pending.)
- :mod:`matofx.migrate.errors` — typed errors carrying the same
  plain-English strings the UI shows and ``MIGRATE_GUIDE.md`` quotes.
  Single source of truth for the message catalog. (Pending.)
- :mod:`matofx.migrate.progress` — streaming status events for the
  UI's live log panel. (Pending.)

Only the ORM lands in this commit; the rest follow in order.
"""

from matofx.migrate.orm import MigrateTrustedPeer
from matofx.migrate.service import (
    ExportService,
    ImportItemOutcome,
    ImportResult,
    ImportService,
    make_export_service,
    make_import_service,
)
from matofx.migrate.trust import TrustedPeerService, make_trusted_peer_service

__all__ = [
    "ExportService",
    "ImportItemOutcome",
    "ImportResult",
    "ImportService",
    "MigrateTrustedPeer",
    "TrustedPeerService",
    "make_export_service",
    "make_import_service",
    "make_trusted_peer_service",
]
