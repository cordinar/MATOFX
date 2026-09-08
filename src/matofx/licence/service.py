"""Installing, re-checking and reporting on the active licence (ADR-0019 slice 2).

Three jobs:

- **Install** — verify a licence file and record it, emitting the audit
  events the compliance report counts.
- **Resolve** — answer "what is this install licensed for, right now?"
  for every other layer. Nothing else is allowed to read the licence
  file directly (brief §C16: the licence module is the single authority
  on what the customer may do).
- **Bridge to compliance** — hand the report its `licence` block, and
  supply the report signing key, which from this slice on lives in the
  manifest rather than the standalone install-identity file.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from matofx.audit import RecordInput, make_audit_writer
from matofx.audit.keys import compute_key_id
from matofx.compliance import IssuedIdentity, LicenceSnapshot
from matofx.licence.errors import LicenceInvalidError, LicenceMissingError
from matofx.licence.loader import LoadedLicence, issuer_required, load_licence_file
from matofx.licence.models import LicenceStatus
from matofx.licence.orm import SINGLETON_ID, LicenceInstall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.config.models import Settings


@dataclass(frozen=True)
class LicenceResolution:
    """What the install is licensed for — or why that can't be answered.

    Exactly one of ``status`` / ``problem`` is set. ``fail_closed``
    distinguishes the two failure modes ADR-0019 §C11 deliberately keeps
    apart: tampered material refuses to serve, a missing file degrades
    to read-only.
    """

    status: LicenceStatus | None = None
    loaded: LoadedLicence | None = None
    problem: str | None = None
    fail_closed: bool = False

    @property
    def read_only(self) -> bool:
        """Whether this state forces read-only mode (slice 6 acts on it).

        **Both kinds of problem stop writes on a running process.** A
        missing licence is an administrative lapse and a tampered one is
        not, but the difference is in how loudly they are reported, not
        in whether the site keeps accepting work.

        This used to return ``not fail_closed``, on the reasoning that a
        tampered licence is handled by refusing to boot rather than by
        degrading. That holds at startup — `check_licence_at_startup`
        raises — but there is no boot in the middle of a process's life.
        Editing `n_slots` in the licence file of a *running* server
        therefore removed the concurrency cap (`status` is None, so
        metering reports unmetered) and left writes flowing, until
        somebody happened to restart. One number in a text file, with no
        restart, was a cheaper bypass than the ceiling §A4 knowingly
        accepts — the same objection §16.57 raised against
        `environment = "dev"`, in its runtime form.

        Metering staying suspended is consistent rather than a hole:
        §C12 suspends CSL accounting in read-only deliberately, because
        an install that cannot submit work has no metered use to protect.
        """
        if self.status is not None:
            return self.status.read_only
        return True

    @property
    def banner(self) -> str | None:
        """The warning to show administrators, if any."""
        if self.status is not None:
            return self.status.banner
        return self.problem


def resolve_licence(settings: Settings, *, now: _dt.datetime | None = None) -> LicenceResolution:
    """Load + verify the configured licence and evaluate its state.

    Never raises: every outcome is a :class:`LicenceResolution`, because
    the callers are startup paths and request middleware that must
    degrade deliberately rather than crash.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    if settings.licence.file is None:
        return LicenceResolution(
            problem=(
                "No licence is configured ([licence].file). The site runs read-only until "
                "one is installed."
            ),
            fail_closed=False,
        )
    try:
        loaded = load_licence_file(
            Path(settings.licence.file),
            extra_issuer_keys=dict(settings.compliance.issuer_public_keys),
            # Decided by the build, not by `environment` — see
            # `loader.issuer_required`. `environment` is customer-editable,
            # so keying this off it made "environment = dev" a one-word
            # bypass of issuer verification entirely.
            require_issuer=issuer_required(settings.environment),
        )
    except LicenceMissingError as exc:
        return LicenceResolution(problem=str(exc), fail_closed=False)
    except LicenceInvalidError as exc:
        # Fail closed: someone edited the licence, or it belongs to
        # another machine. That is not an availability problem.
        return LicenceResolution(problem=str(exc), fail_closed=True)
    return LicenceResolution(status=loaded.manifest.evaluate(now), loaded=loaded)


async def install_licence(
    session: AsyncSession,
    *,
    settings: Settings,
    loaded: LoadedLicence,
    actor: str,
    now: _dt.datetime | None = None,
) -> LicenceInstall:
    """Record ``loaded`` as the active licence and audit the change.

    Idempotent by content: re-installing the same file updates the row
    and still writes the event, because "the admin re-ran install" is
    itself worth seeing in a compliance report.
    """
    now = now or _dt.datetime.now(tz=_dt.UTC)
    manifest = loaded.manifest

    # Refuse a licence for another install *here*, before anything is
    # recorded — rather than letting it install and fail at the next
    # startup. The administrator is standing at the terminal now, and the
    # message tells them exactly what to ask their supplier for.
    if manifest.install_id:
        from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415

        identity = await ensure_install_identity(session, now=now)
        if identity.install_id != manifest.install_id:
            raise LicenceInvalidError(
                f"This licence was issued for install {manifest.install_id!r}, but this is "
                f"install {identity.install_id!r}. Run 'matofx licence request' and ask "
                "your supplier to issue a licence for this install."
            )

    previous = await current_install(session)
    # Snapshot what was there BEFORE mutating: `previous` and `row` are
    # the same ORM object on a replacement, so reading it afterwards
    # would report the new licence as the one it replaced.
    replaced_licence_id = previous.licence_id if previous is not None else None

    row = previous or LicenceInstall(id=SINGLETON_ID)
    row.licence_id = manifest.licence_id
    row.customer = manifest.customer
    row.environment = manifest.environment
    row.licence_type = manifest.licence_type
    row.n_slots = manifest.n_slots
    row.issued_at = manifest.issued_at
    row.expires_at = manifest.expires_at
    row.grace_period_days = manifest.grace_period_days
    row.features = dict(manifest.features)
    row.machine_binding = manifest.machine_binding
    row.banner_text = manifest.banner_text
    row.report_key_id = manifest.report_key_id
    row.issuer_key_id = loaded.issuer_key_id
    row.issuer_verified = loaded.issuer_verified
    row.manifest_sha256 = loaded.file_sha256
    row.source_path = str(loaded.source_path)
    row.installed_at = now
    row.installed_by = actor
    if previous is None:
        session.add(row)
    await session.flush()

    # The licence is what says who the customer is and which report key
    # they were issued, so a self-generated identity gets those fields
    # filled in here rather than at first boot (ADR-0019 §A3).
    if manifest.install_id:
        from matofx.compliance.identity import bind_identity_to_licence  # noqa: PLC0415

        await bind_identity_to_licence(
            session,
            install_id=manifest.install_id,
            customer=manifest.customer,
            environment=manifest.environment,
            issued_at=manifest.issued_at,
            report_key_id=manifest.report_key_id,
            issuer_key_id=loaded.issuer_key_id,
            manifest_sha256=loaded.file_sha256,
        )

    writer = make_audit_writer(settings.audit)
    await writer.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="user" if actor.startswith("user:") else "system",
            action="licence.installed",
            resource_type="licence",
            resource_id=manifest.licence_id,
            outcome="success",
            payload={
                "licence_id": manifest.licence_id,
                "licence_type": manifest.licence_type,
                "n_slots": manifest.n_slots,
                "expires_at": manifest.expires_at.isoformat(),
                "manifest_sha256": loaded.file_sha256,
                "issuer_key_id": loaded.issuer_key_id,
                "issuer_verified": loaded.issuer_verified,
                "replaced_licence_id": replaced_licence_id,
            },
            reason=(
                None
                if replaced_licence_id is None
                else f"replaces {replaced_licence_id}"
                if replaced_licence_id != manifest.licence_id
                else "re-installed"
            ),
        ),
    )
    return row


async def record_validation(
    session: AsyncSession,
    *,
    settings: Settings,
    resolution: LicenceResolution,
    actor: str = "system",
) -> None:
    """Write the periodic-re-check outcome to the audit chain.

    These are the ``licence.validated.ok`` / ``licence.validated.failed``
    counts a compliance report carries: a run of failures is exactly the
    signal that someone has been editing licence material.
    """
    writer = make_audit_writer(settings.audit)
    ok = resolution.status is not None
    await writer.record(
        session,
        RecordInput(
            actor=actor,
            actor_type="system",
            action="licence.validated.ok" if ok else "licence.validated.failed",
            resource_type="licence",
            resource_id=(
                resolution.status.manifest.licence_id if resolution.status else "none"
            ),
            outcome="success" if ok else "failure",
            payload={
                "state": resolution.status.state if resolution.status else None,
                "fail_closed": resolution.fail_closed,
                "read_only": resolution.read_only,
            },
            reason=None if ok else resolution.problem,
        ),
    )


async def current_install(session: AsyncSession) -> LicenceInstall | None:
    """The recorded licence install, or ``None`` on a fresh install."""
    return await session.get(LicenceInstall, SINGLETON_ID)


def licence_snapshot(resolution: LicenceResolution) -> LicenceSnapshot | None:
    """The compliance report's ``licence`` block, or ``None`` when unusable.

    A tampered or absent licence yields ``None``, so the report says
    ``not_installed`` rather than asserting terms it cannot stand behind
    — and the usage figures stay observations rather than becoming
    breaches measured against a cap nobody granted.
    """
    if resolution.status is None or resolution.loaded is None:
        return None
    manifest = resolution.status.manifest
    return LicenceSnapshot(
        licence_id=manifest.licence_id,
        licence_type=manifest.licence_type,
        n_slots=manifest.n_slots,
        issued_at=manifest.issued_at,
        expires_at=manifest.expires_at,
        state=resolution.status.state,
        manifest_sha256=resolution.loaded.file_sha256,
        features_enabled=manifest.enabled_features(),
    )


def report_identity(resolution: LicenceResolution) -> IssuedIdentity | None:
    """The report signing key carried by the licence, if it has one.

    From slice 2 the manifest is the preferred home for this material —
    it arrives through the same signed channel and expires with the
    licence. The standalone install-identity file remains the fallback
    for installs issued before the licence, and for the window between
    delivery and licence install.

    Returns ``None`` (rather than raising) when the licence carries no
    key, so the caller can fall back cleanly.
    """
    if resolution.status is None or resolution.loaded is None:
        return None
    manifest = resolution.status.manifest
    if not manifest.report_key_seed or not manifest.install_id:
        return None

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PrivateKey,
    )

    try:
        private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(manifest.report_key_seed))
    except ValueError as exc:
        raise LicenceInvalidError(
            f"The licence {manifest.licence_id} carries a malformed report key. "
            "Ask your supplier to re-issue it."
        ) from exc
    derived = compute_key_id(private.public_key())
    if manifest.report_key_id and manifest.report_key_id != derived:
        raise LicenceInvalidError(
            f"The licence {manifest.licence_id} declares report key {manifest.report_key_id} "
            f"but carries {derived}. The file has been edited."
        )
    return IssuedIdentity(
        install_id=manifest.install_id,
        customer=manifest.customer,
        environment=manifest.environment,
        issued_at=manifest.issued_at,
        report_key_seed=manifest.report_key_seed,
        report_key_id=derived,
        issuer_key_id=resolution.loaded.issuer_key_id,
        issuer_verified=resolution.loaded.issuer_verified,
        file_sha256=resolution.loaded.file_sha256,
    )


async def verify_install_binding(
    session: AsyncSession, resolution: LicenceResolution
) -> None:
    """Refuse a licence issued for a *different* install (ADR-0019 §A3).

    The manifest has always carried an ``install_id``; nothing compared
    it against the install actually running the licence. So copying the
    licence file to a second machine and installing MATOFX fresh simply
    worked — same slots, same features, second install — and the only
    trace was a report counter collision, which needs *both* installs to
    eventually report before anyone notices.

    Binding to the install rather than to the host is the deliberate
    choice (maintainer, 2026-08-11). ``install_id`` lives in the database,
    so it survives container rebuilds, VM moves and host migrations —
    exactly the events that break ``machine_binding`` and generate a
    reissue. Machine binding remains available for hosts that never move,
    as an issuer-chosen extra.

    What this does **not** stop is a clone of the whole database, which
    carries the ``install_id`` with it. Nothing local can: anything the
    install stores can be copied. That case remains detection-only
    (§A3 counter collision), and a licence server on the customer's own
    network is the known upgrade if it ever becomes commercially
    material.

    **Never skipped.** The install mints its own identity on first boot,
    so there is always something to compare against. The earlier version
    returned early when no identity had been adopted — and since adoption
    only ever happened by hand, a second instance that simply never ran
    ``compliance adopt`` was exempt from binding for ever. That was the
    hole this check was supposed to close.

    Raises:
        LicenceInvalidError: The licence belongs to another install.
    """
    from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415

    if resolution.status is None:
        return
    manifest = resolution.status.manifest
    if not manifest.install_id:
        # A licence with no install_id predates binding, or is a bare
        # manifest. Nothing to enforce — the loader's other gates stand.
        return

    identity = await ensure_install_identity(session)
    if identity.install_id == manifest.install_id:
        return

    raise LicenceInvalidError(
        f"The licence {manifest.licence_id} was issued for install "
        f"{manifest.install_id!r}, but this is install {identity.install_id!r}. A licence "
        "is issued for one install and cannot be moved to another. Run "
        "'matofx licence request' to get this install's id, and ask your supplier to "
        "issue a licence for it. (If you have genuinely rebuilt this deployment from "
        "scratch, its records and identity were rebuilt too, and your supplier needs to "
        "know.)"
    )
