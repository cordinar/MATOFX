"""Typed errors for the cross-install migration domain.

Each error carries a plain-English ``detail`` string that is safe to
render to admins. **These strings are part of the public surface of
the feature** — the UI quotes them verbatim in the import status log,
the CLI prints them to stderr, and ``MIGRATE_GUIDE.md`` §6
catalogues them. Treat any reword as a public-API change.

Two unrelated error namespaces share this module:

* :class:`MigrateError` and its subclasses — service-layer domain
  errors (peer not found, peer already trusted, bundle from an
  untrusted source, etc.). These are what the trust service and the
  import service raise after their input is structurally valid.

* :class:`matofx.migrate.bundle.BundleError` and
  :class:`matofx.migrate.signing.BundleSigningError` — pure-bundle
  errors raised by the bundle / signing modules during pack and
  unpack. Defined in those modules so the pure layer has no upward
  dependency. They surface alongside the domain errors below in the
  import-status-log catalog.
"""

from __future__ import annotations


class MigrateError(RuntimeError):
    """Base class for migration-domain errors. Carries a user-facing ``detail``."""

    def __init__(self, detail: str) -> None:
        """Construct with the user-facing reason."""
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# Trust-list errors
# ---------------------------------------------------------------------------


class TrustPeerNotFoundError(MigrateError):
    """No trust entry exists for the supplied fingerprint."""

    def __init__(self, fingerprint: str) -> None:
        """Construct for ``fingerprint`` (already canonicalised)."""
        super().__init__(
            f"No trusted peer exists with fingerprint {fingerprint!r}. "
            "Check the Trusted peers panel — the entry may have been "
            "removed already, or you may be looking at a different "
            "install's trust list."
        )
        self.fingerprint = fingerprint


class TrustPeerAlreadyTrustedError(MigrateError):
    """The supplied fingerprint is already in the trust list."""

    def __init__(self, fingerprint: str, existing_label: str) -> None:
        """Construct for the canonical fingerprint and the row's existing label."""
        super().__init__(
            f"This fingerprint is already trusted under the label "
            f"{existing_label!r}. If you wanted to change the label, "
            "remove the existing entry first and add it again."
        )
        self.fingerprint = fingerprint
        self.existing_label = existing_label


# ---------------------------------------------------------------------------
# Import-time errors (raised by the import path; defined here so the
# message catalogue stays in one file)
# ---------------------------------------------------------------------------


class BundleUntrustedError(MigrateError):
    """The bundle's signature verified, but its fingerprint isn't trusted.

    The bundle is authentically signed by some install — just not one
    this install has been told to trust. The fix is for the admin to
    add the fingerprint to the trust list (and only after they have
    verified out-of-band that the fingerprint really does belong to
    the install they think it does).
    """

    def __init__(self, fingerprint_display: str) -> None:
        """Construct with the *display-form* fingerprint (colon-uppercase)."""
        super().__init__(
            f"This bundle was signed by an install that is not in this "
            f"install's trusted peer list (fingerprint "
            f"{fingerprint_display}). To accept it, an administrator "
            "must first add this fingerprint to the trust list "
            "(Admin -> Migrate Tools/Workflows -> Trusted peers -> "
            "Add trusted peer). Import is blocked."
        )
        self.fingerprint_display = fingerprint_display


# ---------------------------------------------------------------------------
# Per-item import skip reasons
# ---------------------------------------------------------------------------
#
# These are NOT raised to abort the bundle import — they are caught by
# the per-item dispatcher and rendered as a skipped-with-reason line in
# the status log. The bundle as a whole keeps going.
#
# Each carries the alias/version/etc. it skipped, plus a plain-English
# ``detail`` field aligned with MIGRATE_GUIDE.md §6. The detail string
# is what surfaces in the UI's live status log and the CLI stderr.


class ItemSkippedError(MigrateError):
    """Base class for "this single item could not be imported" reasons.

    Carries enough structured detail (kind, alias, version) for the
    progress event to render a clean log line, alongside the
    plain-English ``detail`` the operator reads.
    """

    def __init__(
        self,
        *,
        kind: str,
        alias: str,
        version: str | None,
        detail: str,
    ) -> None:
        """Construct with the item identity + plain-English reason."""
        super().__init__(detail)
        self.kind = kind
        self.alias = alias
        self.version = version


class VersionHashConflictError(ItemSkippedError):
    """``(alias, version)`` already exists on dest with a different descriptor.

    Wording matches MIGRATE_GUIDE.md §6.2.
    """

    def __init__(self, alias: str, version: str) -> None:
        """Construct for the colliding tool version."""
        super().__init__(
            kind="tool",
            alias=alias,
            version=version,
            detail=(
                f"Tool {alias} version {version} already exists here with "
                "a different descriptor. This usually means somebody "
                "edited the tool on the source after this install first "
                "received an earlier version of it. Bump the version on "
                "the source (e.g. to a patch release) and re-export the "
                "bundle, then retry the import."
            ),
        )


class MissingReferenceError(ItemSkippedError):
    """A by-name reference (AOS host / HPC cluster / Python env / storage
    location / licence) the imported tool needs does not exist on the
    destination.

    Wording matches MIGRATE_GUIDE.md §6.3.
    """

    def __init__(
        self,
        *,
        alias: str,
        version: str,
        ref_kind: str,
        ref_name: str,
        where_to_create: str,
    ) -> None:
        """Construct for one unresolved by-name reference."""
        super().__init__(
            kind="tool",
            alias=alias,
            version=version,
            detail=(
                f"Tool {alias} {version} is bound on the source to "
                f"{ref_kind} {ref_name!r}, which does not exist on this "
                f"install. Create it first under {where_to_create}, then "
                "re-import the bundle."
            ),
        )
        self.ref_kind = ref_kind
        self.ref_name = ref_name


class ApproverListEmptyError(ItemSkippedError):
    """A tool's approver list is empty after dropping unmatched usernames,
    and its ``approvals_policy`` requires at least one approver.

    The descriptor validator refuses an empty approver list when the
    policy is anything other than ``"none"``. Rather than coerce the
    policy down to ``"none"`` (which would be a security regression —
    the source admin set the policy deliberately), we skip the tool
    and ask the operator to seed the missing approver user on the
    destination first.
    """

    def __init__(
        self,
        *,
        alias: str,
        version: str,
        dropped_usernames: list[str],
        approvals_policy: str,
    ) -> None:
        """Construct with the alias / dropped list / source policy."""
        listed = ", ".join(repr(u) for u in dropped_usernames) or "(none specified)"
        super().__init__(
            kind="tool",
            alias=alias,
            version=version,
            detail=(
                f"Tool {alias} {version} has an approvals policy of "
                f"{approvals_policy!r}, which requires at least one "
                "approver. The source's approver usernames don't exist "
                f"on this install (missing: {listed}), so the approver "
                "list would be empty. Create the matching user "
                "account(s) here (Admin -> Users -> New user), then "
                "re-import the bundle."
            ),
        )
        self.dropped_usernames = dropped_usernames
        self.approvals_policy = approvals_policy


class WorkflowAliasCollisionError(ItemSkippedError):
    """A workflow with the same alias already exists on the destination.

    Workflows don't have a version axis so name collisions are a hard
    skip — the admin must rename one side. Wording matches
    MIGRATE_GUIDE.md §6.5.
    """

    def __init__(self, alias: str) -> None:
        """Construct for the colliding workflow alias."""
        super().__init__(
            kind="workflow",
            alias=alias,
            version=None,
            detail=(
                f"Workflow {alias!r} already exists on this install. "
                "Rename the workflow on the source (or remove the "
                "existing one here, if it really is obsolete) and "
                "re-import."
            ),
        )


class WorkflowDependencyMissingError(ItemSkippedError):
    """A workflow's member tool was not imported and is not already
    present on the destination.

    Cascading skip — fires when a tool earlier in the same bundle was
    itself skipped, or when the bundle references a tool that simply
    isn't here. Wording matches MIGRATE_GUIDE.md §6.4.
    """

    def __init__(self, workflow_alias: str, tool_alias: str) -> None:
        """Construct with the workflow + the missing tool reference."""
        super().__init__(
            kind="workflow",
            alias=workflow_alias,
            version=None,
            detail=(
                f"Workflow {workflow_alias!r} depends on tool "
                f"{tool_alias!r}, which was not imported (see earlier "
                "log entries) and is not already installed here. Resolve "
                "the missing tool and re-import the bundle to pick up "
                "this workflow."
            ),
        )
        self.tool_alias = tool_alias


class ToolNotFoundForExportError(MigrateError):
    """The export caller asked for ``(alias, version)`` that isn't installed.

    Raised at *export* time, not import — refusing to ship a bundle
    that's incomplete is cheaper than letting the destination admin
    discover the gap later.
    """

    def __init__(self, alias: str, version: str) -> None:
        """Construct for the missing tool."""
        super().__init__(
            f"No tool {alias} version {version} is installed on this "
            "install, so it cannot be exported. Check the picker — the "
            "version may have been bumped, or the tool may have been "
            "archived."
        )
        self.alias = alias
        self.version = version


class WorkflowNotFoundForExportError(MigrateError):
    """The export caller asked for a workflow alias that isn't installed."""

    def __init__(self, alias: str) -> None:
        """Construct for the missing workflow."""
        super().__init__(
            f"No admin-released workflow with alias {alias!r} is "
            "installed on this install, so it cannot be exported. "
            "(Custom workflows are not exportable in v1 — only "
            "admin-released workflows.)"
        )
        self.alias = alias
