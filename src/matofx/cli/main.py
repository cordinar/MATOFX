"""Entry point for the ``matofx`` admin CLI.

Phase 0 / Phase 1 ship ``matofx --version``, ``matofx audit verify``,
and ``matofx admin create-first-user``. Remaining subcommands (doctor,
support bundle, backup/restore, upgrade, tool install/update/uninstall,
licence install/reload, tls, rotate signing-key, tpm enrol) land
alongside the modules they operate on, per the brief §I46.

The CLI is built on stdlib ``argparse`` for now. We'll switch to Click
when the third subcommand lands, to avoid premature framework choice.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from matofx import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.config import Settings
    from matofx.licence import LicenceResolution


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    parsed = parser.parse_args(args)

    if parsed.command is None:
        print(f"matofx {__version__} — admin CLI")
        parser.print_help()
        return 0

    dispatch = {
        "audit": _dispatch_audit,
        "admin": _dispatch_admin,
        "compliance": _dispatch_compliance,
        "licence": _dispatch_licence,
        "migrate": _dispatch_migrate,
        "rotate": _dispatch_rotate,
        "secrets": _dispatch_secrets,
    }
    handler = dispatch.get(parsed.command)
    if handler is None:
        parser.error(f"Unknown command: {parsed.command}")
        return 2  # unreachable; argparse exits

    from matofx.config.loader import ConfigError  # noqa: PLC0415
    from matofx.licence import LicenceError  # noqa: PLC0415

    try:
        return handler(parsed)
    except ConfigError as exc:
        # The loader's message already names the offending key, the
        # reason, and the fix. Wrapping it in a Python traceback buries
        # all three under a stack the operator cannot act on — and a
        # misconfigured install is exactly when the CLI is being run by
        # somebody who has never seen this codebase.
        print(f"matofx: {exc}", file=sys.stderr)
        return 2
    except LicenceError as exc:
        # Same reasoning, and the case that matters most: a licence
        # issued for another install is refused deep inside
        # `install_licence`, past the handler's own pre-copy check. Its
        # message names both ids and tells the administrator to run
        # `matofx licence request` — which is unreadable at the bottom of
        # a 25-line stack, at go-live, in front of the customer.
        print(f"matofx: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 — a flat sequence of sub-command registrations.
    parser = argparse.ArgumentParser(
        prog="matofx",
        description="MATOFX SPDM administrative CLI.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"matofx {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    audit = sub.add_parser("audit", help="Audit log operations.")
    audit_sub = audit.add_subparsers(dest="audit_command", metavar="SUBCOMMAND")

    verify = audit_sub.add_parser("verify", help="Verify the audit chain integrity.")
    verify.add_argument("--start-id", type=int, default=None, help="Inclusive start row id.")
    verify.add_argument("--end-id", type=int, default=None, help="Inclusive end row id.")

    admin = sub.add_parser("admin", help="Administrative commands.")
    admin_sub = admin.add_subparsers(dest="admin_command", metavar="SUBCOMMAND")
    create_first = admin_sub.add_parser(
        "create-first-user",
        help="Create the first super-user on a fresh install.",
    )
    create_first.add_argument("--username", required=True)
    create_first.add_argument("--email", required=True)
    create_first.add_argument("--first-name", required=True)
    create_first.add_argument("--last-name", required=True)
    create_first.add_argument(
        "--password",
        default=None,
        help="Password (omit to read from stdin; safer in shell history terms).",
    )
    admin_sub.add_parser(
        "backfill-validity",
        help="Recompute output validity for existing study results (ADR-0017).",
    )

    _add_licence_subparser(sub)
    _add_compliance_subparser(sub)

    # ----- migrate -----------------------------------------------------
    migrate = sub.add_parser(
        "migrate",
        help="Cross-install Tools/Workflows migration (export + import + trust).",
    )
    migrate_sub = migrate.add_subparsers(dest="migrate_command", metavar="SUBCOMMAND")

    migrate_sub.add_parser(
        "fingerprint",
        help="Print this install's Ed25519 fingerprint (display form).",
    )

    trust = migrate_sub.add_parser("trust", help="Manage the trusted source-install list.")
    trust_sub = trust.add_subparsers(dest="trust_command", metavar="SUBCOMMAND")
    trust_sub.add_parser("list", help="List trusted source installs.")
    trust_add = trust_sub.add_parser("add", help="Add a trusted source install.")
    trust_add.add_argument("fingerprint", help="The source's fingerprint (any format).")
    trust_add.add_argument("--label", required=True, help="Human label, e.g. 'Staging'.")
    trust_add.add_argument("--notes", default="", help="Optional free-text note.")
    trust_add.add_argument("--user", required=True, help="Username acting (audit attribution).")
    trust_remove = trust_sub.add_parser("remove", help="Remove a trusted source install.")
    trust_remove.add_argument("fingerprint", help="The fingerprint to remove.")
    trust_remove.add_argument(
        "--reason",
        required=True,
        help="Plain-English why-text (captured in the audit row).",
    )
    trust_remove.add_argument("--user", required=True, help="Username acting (audit attribution).")

    export = migrate_sub.add_parser(
        "export", help="Build a signed migration bundle of tools + workflows."
    )
    export.add_argument(
        "--tools",
        default="",
        help="Comma-separated 'alias:version' pairs to bundle.",
    )
    export.add_argument(
        "--workflows",
        default="",
        help="Comma-separated workflow aliases to bundle.",
    )
    export.add_argument(
        "--notes", default="", help="Optional free-text note recorded in the manifest."
    )
    export.add_argument(
        "--include-required",
        action="store_true",
        help="Also bundle workflow members and the upstream tools the chosen tools are "
        "wired from (their current versions).",
    )
    export.add_argument("--out", required=True, help="Output bundle path (.tar.gz).")
    export.add_argument("--user", required=True, help="Username acting (audit attribution).")

    import_p = migrate_sub.add_parser("import", help="Verify and import a migration bundle.")
    import_p.add_argument("bundle", help="Path to the .tar.gz bundle to import.")
    import_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk the import and report outcomes without writing.",
    )
    import_p.add_argument("--user", required=True, help="Username acting (audit attribution).")

    # ----- rotate ------------------------------------------------------
    rotate = sub.add_parser("rotate", help="Rotate cryptographic keys.")
    rotate_sub = rotate.add_subparsers(dest="rotate_command", metavar="SUBCOMMAND")
    rotate_key = rotate_sub.add_parser(
        "signing-key",
        help="Initialise (--init) or rotate the audit signing key.",
    )
    rotate_key.add_argument(
        "--init",
        action="store_true",
        help="Create the first signing key (fails if one already exists).",
    )
    rotate_key.add_argument(
        "--path",
        default=None,
        help="Key file path (defaults to audit.signing_key_path from matofx.toml).",
    )

    _add_secrets_subparser(sub)

    return parser


def _add_secrets_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Attach the ``matofx secrets {init|set|get|list|remove}`` subcommands."""
    secrets = sub.add_parser(
        "secrets",
        help="Manage the encrypted secrets store (AOS / HPC credentials).",
    )
    secrets_sub = secrets.add_subparsers(dest="secrets_command", metavar="SUBCOMMAND")
    secrets_sub.add_parser(
        "init",
        help="Generate a new master key to paste into [secrets].master_key.",
    )
    s_set = secrets_sub.add_parser("set", help="Add or replace a secret by reference.")
    s_set.add_argument("ref", help="The opaque secret_ref (matches the host / cluster row).")
    s_set.add_argument(
        "--value",
        default=None,
        help="The secret value (omit to read from a hidden prompt / stdin).",
    )
    s_get = secrets_sub.add_parser("get", help="Print a stored secret (reveals the value).")
    s_get.add_argument("ref", help="The secret_ref to reveal.")
    secrets_sub.add_parser("list", help="List stored secret references (non-secret).")
    s_remove = secrets_sub.add_parser("remove", help="Remove a stored secret by reference.")
    s_remove.add_argument("ref", help="The secret_ref to remove.")


def _add_licence_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Attach ``matofx licence {install|reload|show}`` (brief §C16)."""
    licence = sub.add_parser(
        "licence",
        help="Install, re-check and inspect this install's licence.",
    )
    licence_sub = licence.add_subparsers(dest="licence_command", metavar="SUBCOMMAND")

    install = licence_sub.add_parser(
        "install",
        help="Verify a licence file and activate it (refuses an invalid one).",
    )
    install.add_argument("path", help="Path to the signed licence JSON.")
    install.add_argument(
        "--user",
        default=None,
        help="Username acting, for audit attribution (default: recorded as 'cli').",
    )
    install.add_argument(
        "--keep-in-place",
        action="store_true",
        help="Don't copy the file to [licence].file — activate it where it is.",
    )

    licence_sub.add_parser(
        "reload",
        help="Re-read + re-verify the configured licence file (records the outcome).",
    )
    licence_sub.add_parser("show", help="Print the current licence state.")
    licence_sub.add_parser(
        "request",
        help="Print this install's id, to send to your supplier for a licence.",
    )


def _dispatch_licence(parsed: argparse.Namespace) -> int:
    command = getattr(parsed, "licence_command", None)
    if command == "install":
        return asyncio.run(
            _licence_install(
                path_arg=parsed.path,
                username=parsed.user,
                keep_in_place=parsed.keep_in_place,
            )
        )
    if command == "reload":
        return asyncio.run(_licence_reload())
    if command == "show":
        return asyncio.run(_licence_show())
    if command == "request":
        return asyncio.run(_licence_request())
    print("Usage: matofx licence {install <path>|reload|show|request}", file=sys.stderr)
    return 2


async def _licence_install(*, path_arg: str, username: str | None, keep_in_place: bool) -> int:
    import shutil  # noqa: PLC0415

    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.licence import LicenceError, install_licence, load_licence_file  # noqa: PLC0415
    from matofx.licence.loader import issuer_required  # noqa: PLC0415

    settings = load_settings()
    source = Path(path_arg)
    try:
        # Verify BEFORE copying anything into place: an invalid licence
        # must never displace a working one.
        loaded = load_licence_file(
            source,
            extra_issuer_keys=dict(settings.compliance.issuer_public_keys),
            require_issuer=issuer_required(settings.environment),
        )
    except LicenceError as exc:
        print(f"licence install: {exc}", file=sys.stderr)
        return 1

    destination = source
    if not keep_in_place and settings.licence.file is not None:
        destination = Path(settings.licence.file)
        if destination.resolve() != source.resolve():
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            except OSError as exc:
                print(f"licence install: could not write {destination}: {exc}", file=sys.stderr)
                return 1
        loaded = load_licence_file(
            destination,
            extra_issuer_keys=dict(settings.compliance.issuer_public_keys),
            require_issuer=issuer_required(settings.environment),
        )

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            actor = "cli"
            if username:
                actor = f"user:{await _resolve_user_id(session, username)}"
            await install_licence(session, settings=settings, loaded=loaded, actor=actor)

    manifest = loaded.manifest
    status = manifest.evaluate(_utcnow())
    print(f"installed licence {manifest.licence_id} for {manifest.customer}")
    print(f"  type    : {manifest.licence_type.upper()} — {manifest.n_slots} slot(s)")
    print(f"  expires : {manifest.expires_at:%Y-%m-%d} ({status.state}, {status.days_remaining}d)")
    print(f"  file    : {destination}")
    if not loaded.issuer_verified:
        print(
            "  warning: the issuer signature could not be verified against a trusted key.",
            file=sys.stderr,
        )
    if manifest.machine_binding:
        from matofx.licence.machine import looks_containerised  # noqa: PLC0415

        if looks_containerised():
            # The licence is valid *now*; it stops being valid the next
            # time this container is rebuilt, because machine binding
            # compares an OS machine id and a rebuild mints a new one.
            # Far better said here than discovered mid-deployment.
            print(
                "  warning: this licence is tied to this machine, and this install "
                "looks like a container. Machine binding does not survive a container "
                "rebuild — the licence will be refused the next time this container is "
                "recreated. Ask your supplier for a licence without machine binding; "
                "it is still tied to this MATOFX install.",
                file=sys.stderr,
            )
    return 0


async def _licence_reload() -> int:
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.licence import record_validation, resolve_licence  # noqa: PLC0415

    settings = load_settings()
    resolution = resolve_licence(settings)
    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await record_validation(session, settings=settings, resolution=resolution)

    _print_licence_resolution(resolution)
    return 0 if resolution.status is not None else 1


async def _licence_request() -> int:
    """Print this install's id for the supplier to issue a licence against.

    The enrolment half of ADR-0019 §A3: the install names itself, and
    Funis issues a licence *for* that name. Minting the id here (rather
    than waiting for a licence to supply one) is what makes a second
    deployment refuse to start — it generates a different id, and the
    licence does not name it.

    Safe to run repeatedly: the id is created once and then simply read.
    """
    from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.licence import machine_fingerprint  # noqa: PLC0415

    settings = load_settings()
    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            identity = await ensure_install_identity(session)
            install_id = identity.install_id
            known_customer = identity.customer

    print("Send this to your supplier to have a licence issued:")
    print()
    print(f"  install id : {install_id}")
    fingerprint = machine_fingerprint()
    if fingerprint is not None:
        # Only needed if the supplier is issuing a machine-bound licence;
        # printed either way so one exchange is enough.
        print(f"  machine    : {fingerprint[:32]}  (only needed for a machine-bound licence)")
    if known_customer:
        print(f"  licensed to: {known_customer}")
    print()
    print(
        "Every licence for this install must name this id. Keep it — a replacement "
        "or renewal licence has to carry the same one, or MATOFX will refuse to start."
    )
    return 0


async def _licence_show() -> int:
    """Read-only view — never writes, so it needs no database session."""
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.licence import resolve_licence  # noqa: PLC0415

    settings = load_settings()
    resolution = resolve_licence(settings)
    _print_licence_resolution(resolution, settings=settings)
    return 0 if resolution.status is not None else 1


def _print_licence_resolution(
    resolution: LicenceResolution, *, settings: Settings | None = None
) -> None:
    """Render a :class:`~matofx.licence.LicenceResolution` for the terminal.

    Reports the features the install will **actually run with**, not the
    raw manifest map. The two differ in both directions, and this is the
    command an administrator uses to answer "why is this feature
    missing?" — so printing the manifest was worse than printing nothing:

    - an empty ``features`` map means "this licence expresses no opinion"
      and the TOML settings apply, so the manifest can list nothing while
      the install runs several features;
    - a slug this build doesn't have is ignored at the gate, so the
      manifest can list a feature that will never appear.
    """
    status = resolution.status
    if status is None:
        mode = "REFUSING TO SERVE" if resolution.fail_closed else "READ-ONLY"
        print(f"licence: NOT USABLE ({mode})")
        print(f"  {resolution.problem}")
        return
    manifest = status.manifest
    print(f"licence: {status.state.upper()} — {manifest.licence_id} ({manifest.customer})")
    print(f"  type    : {manifest.licence_type.upper()} — {manifest.n_slots} slot(s)")
    print(f"  issued  : {manifest.issued_at:%Y-%m-%d}")
    print(f"  expires : {manifest.expires_at:%Y-%m-%d} (grace {manifest.grace_period_days}d)")
    _print_features(manifest, resolution, settings)
    if manifest.machine_binding:
        print("  bound   : yes (machine-bound licence)")
    if resolution.loaded is not None and not resolution.loaded.issuer_verified:
        print(
            "  issuer  : NOT VERIFIED — this licence carries no signature this build "
            "trusts. Every compliance report will say so."
        )
    if status.banner:
        print(f"  ! {status.banner}")


def _print_features(
    manifest: object, resolution: LicenceResolution, settings: Settings | None
) -> None:
    """The effective feature set, and why it differs from the manifest."""
    from matofx.features import Feature  # noqa: PLC0415

    requested = list(getattr(manifest, "enabled_features", list)())
    if settings is None:  # pragma: no cover - callers always pass settings
        print(f"  features: {', '.join(requested) or '(none)'}")
        return

    from matofx.licence.features import effective_feature_settings  # noqa: PLC0415

    effective = effective_feature_settings(settings, resolution).enabled
    print(f"  features: {', '.join(effective) or '(none)'}")

    if not requested and effective:
        print(
            "            (the licence names no features, so the [features] block in "
            "matofx.toml applies)"
        )
    unknown = sorted(set(requested) - {f.value for f in Feature})
    if unknown:
        print(
            f"            (ignored, not in this build: {', '.join(unknown)} — upgrade "
            "MATOFX to use them)"
        )


def _add_compliance_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Attach ``matofx compliance {adopt|report|verify}`` (ADR-0019)."""
    compliance = sub.add_parser(
        "compliance",
        help="Licence-compliance attestation: adopt an identity, generate + check reports.",
    )
    compliance_sub = compliance.add_subparsers(dest="compliance_command", metavar="SUBCOMMAND")

    adopt = compliance_sub.add_parser(
        "adopt",
        help="Adopt the Funis-issued install-identity file (run once per install).",
    )
    adopt.add_argument("path", help="Path to funis-install-identity.json.")

    report = compliance_sub.add_parser(
        "report",
        help="Generate a signed compliance report for Funis.",
    )
    report.add_argument("--out", default=None, help="Output .json path (default: auto-named).")
    report.add_argument(
        "--no-html",
        action="store_true",
        help="Skip the human-readable .html twin written beside the .json.",
    )
    report.add_argument(
        "--since",
        default=None,
        help="ISO-8601 start of the period (default: the previous report's timestamp).",
    )
    report.add_argument(
        "--prove-from",
        type=int,
        default=None,
        metavar="SIZE",
        help=(
            "Also prove consistency from this record count. Use when your supplier "
            "says a report carried no proof from the size they hold."
        ),
    )

    verify_report_cmd = compliance_sub.add_parser(
        "verify",
        help="Funis-side: check a returned report against the head ledger.",
    )
    verify_report_cmd.add_argument("path", help="The report .json received from the customer.")
    verify_report_cmd.add_argument(
        "--ledger",
        required=True,
        help="Path to the append-only JSONL head ledger for your installs.",
    )
    verify_report_cmd.add_argument(
        "--public-key",
        default=None,
        help="Hex Ed25519 report key — needed only for an install's first report.",
    )
    verify_report_cmd.add_argument(
        "--append",
        action="store_true",
        help="Record this report in the ledger after a clean check.",
    )
    verify_report_cmd.add_argument(
        "--ledger-key",
        default=None,
        metavar="PEM",
        help=(
            "Funis ledger signing key, used to sign the appended entry. "
            "Required once the ledger holds signed entries."
        ),
    )
    verify_report_cmd.add_argument(
        "--ledger-key-id",
        default=None,
        help="Ledger key id (default: the key filename stem).",
    )


def _dispatch_compliance(parsed: argparse.Namespace) -> int:
    command = getattr(parsed, "compliance_command", None)
    if command == "adopt":
        return asyncio.run(_compliance_adopt(parsed.path))
    if command == "report":
        return asyncio.run(
            _compliance_report(
                out=parsed.out,
                write_html=not parsed.no_html,
                since=parsed.since,
                prove_from=parsed.prove_from,
            )
        )
    if command == "verify":
        return _compliance_verify(
            path=parsed.path,
            ledger_path=parsed.ledger,
            public_key=parsed.public_key,
            append=parsed.append,
            ledger_key=parsed.ledger_key,
            ledger_key_id=parsed.ledger_key_id,
        )
    print(
        "Usage: matofx compliance {adopt <file>|report [--out F]|verify <file> --ledger L}",
        file=sys.stderr,
    )
    return 2


def _load_issued_identity(settings: object) -> object:
    """The report signing identity — licence manifest first, file second.

    From ADR-0019 slice 2 the licence carries this material: it arrives
    through the same signed channel and expires with the licence. The
    standalone install-identity file remains the fallback for installs
    issued before their licence, and for the delivery window in between.
    """
    from matofx.compliance import load_identity_file  # noqa: PLC0415
    from matofx.compliance.errors import InstallIdentityError  # noqa: PLC0415
    from matofx.licence import report_identity, resolve_licence  # noqa: PLC0415
    from matofx.licence.loader import issuer_required  # noqa: PLC0415

    from_licence = report_identity(resolve_licence(settings))  # type: ignore[arg-type]
    if from_licence is not None:
        return from_licence

    identity_file = settings.compliance.identity_file  # type: ignore[attr-defined]
    if identity_file is None:
        raise InstallIdentityError(
            "No report signing key available: the installed licence carries none, and "
            "[compliance].identity_file is not configured. Point it at the install-identity "
            "file Funis issued for this install."
        )
    return load_identity_file(
        Path(identity_file),
        extra_issuer_keys=dict(settings.compliance.issuer_public_keys),  # type: ignore[attr-defined]
        require_issuer=issuer_required(settings.environment),  # type: ignore[attr-defined]
    )


async def _compliance_adopt(path_arg: str) -> int:
    from matofx.compliance import adopt_identity, load_identity_file  # noqa: PLC0415
    from matofx.compliance.errors import ComplianceError  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.licence.loader import issuer_required  # noqa: PLC0415

    settings = load_settings()
    try:
        issued = load_identity_file(
            Path(path_arg),
            extra_issuer_keys=dict(settings.compliance.issuer_public_keys),
            require_issuer=issuer_required(settings.environment),
        )
    except ComplianceError as exc:
        print(f"compliance adopt: {exc}", file=sys.stderr)
        return 1

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            try:
                row = await adopt_identity(session, issued, now=_utcnow())
            except ComplianceError as exc:
                print(f"compliance adopt: {exc}", file=sys.stderr)
                return 1
            install_id = row.install_id
            counter = row.report_counter

    print(f"adopted install {install_id} for {issued.customer}")
    print(f"  report key : {issued.report_key_id}")
    print(f"  public key : {issued.public_key_hex()}")
    suffix = "" if issued.issuer_verified else " (UNVERIFIED)"
    print(f"  issuer     : {issued.issuer_key_id}{suffix}")
    print(f"  reports so far: {counter}")
    if not issued.issuer_verified:
        print(
            "  note: the issuer signature could not be verified against a trusted key; "
            "every report will say so.",
            file=sys.stderr,
        )
    return 0


async def _compliance_report(
    *,
    out: str | None,
    write_html: bool,
    since: str | None,
    prove_from: int | None = None,
) -> int:
    import json as _json  # noqa: PLC0415

    from matofx.compliance import generate_report, render_report_html  # noqa: PLC0415
    from matofx.compliance.errors import ComplianceError  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.licence import LicenceError, licence_snapshot, resolve_licence  # noqa: PLC0415

    settings = load_settings()
    try:
        issued = _load_issued_identity(settings)
        period_from = _dt.datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    except (ComplianceError, LicenceError, ValueError) as exc:
        print(f"compliance report: {exc}", file=sys.stderr)
        return 1

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            try:
                generated = await generate_report(
                    session,
                    settings=settings,
                    issued=issued,  # type: ignore[arg-type]
                    actor="cli",
                    period_from=period_from,
                    licence=licence_snapshot(resolve_licence(settings)),
                    prove_from=prove_from,
                )
            except ComplianceError as exc:
                print(f"compliance report: {exc}", file=sys.stderr)
                return 1

    document = generated.document
    install = document.get("install", {})
    stamp = str(document.get("generated_at", ""))[:10]
    default_name = f"compliance-{install.get('install_id', 'install')}-{stamp}.json"
    target = Path(out) if out else Path(default_name)
    target.write_text(
        _json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    if prove_from is not None:
        # Asked to anchor to a specific size and it was never attested:
        # say so. Otherwise the operator sends this to their supplier
        # believing it answers the request, and the verifier reports the
        # same missing proof as before — a round trip wasted, and the
        # customer looking evasive for no reason.
        carried = {
            int(entry.get("tree_size", -1))
            for entry in (document.get("anchor", {}).get("consistency_proofs") or [])
        }
        if prove_from not in carried:
            print(
                f"warning: this install has never attested a {prove_from}-record tree, "
                "so the report carries no proof from it. Check the size in your "
                "supplier's ledger entry.",
                file=sys.stderr,
            )
    print(f"wrote {target} (report #{generated.counter}, sha256 {generated.content_sha256[:12]}…)")
    if write_html:
        html_path = target.with_suffix(".html")
        html_path.write_text(render_report_html(document), encoding="utf-8")
        print(f"wrote {html_path} (human-readable twin)")

    chain = document.get("chain_verification", {})
    if chain.get("ok") is not True:
        print(
            "warning: this install's own chain verification FAILED — the report says so, "
            "which is the point. Investigate with 'matofx audit verify'.",
            file=sys.stderr,
        )
    return 0


class _FileLedgerSigner:
    """An encrypted PKCS#8 ledger key on disk.

    Deliberately the same shape as ``scripts/issue_licence.py``'s signer
    so one hardware token could later serve both, and deliberately *not*
    the same key: the issuing key should live offline, while the ledger
    key is used every time a report is received.
    """

    def __init__(self, key_id: str, private: Ed25519PrivateKey) -> None:
        self.key_id = key_id
        self._private = private

    def sign(self, payload: bytes) -> bytes:
        return self._private.sign(payload)


def _load_ledger_signer(key_path: str | None, key_id: str | None) -> _FileLedgerSigner | None:
    """Load the ledger signing key, or ``None`` when none was supplied."""
    if key_path is None:
        return None

    import getpass  # noqa: PLC0415

    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PrivateKey,
    )

    from matofx.compliance.errors import LedgerError  # noqa: PLC0415

    source = Path(key_path)
    passphrase = os.environ.get("MATOFX_LEDGER_PASSPHRASE")
    secret = passphrase.encode("utf-8") if passphrase else None
    try:
        loaded = serialization.load_pem_private_key(
            source.read_bytes(),
            password=secret or getpass.getpass("Ledger key passphrase: ").encode("utf-8"),
        )
    except OSError as exc:
        raise LedgerError(f"Could not read ledger key {source}: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"Could not unlock ledger key {source}: {exc}") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise LedgerError(f"{source} is not an Ed25519 private key.")
    return _FileLedgerSigner(key_id or source.stem, loaded)


def _compliance_verify(
    *,
    path: str,
    ledger_path: str,
    public_key: str | None,
    append: bool,
    ledger_key: str | None = None,
    ledger_key_id: str | None = None,
) -> int:
    import json as _json  # noqa: PLC0415

    from matofx.compliance import (  # noqa: PLC0415
        append_entry,
        entry_from_report,
        read_ledger,
        verify_report,
    )
    from matofx.compliance.errors import ComplianceError  # noqa: PLC0415

    try:
        document = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"compliance verify: could not read {path}: {exc}", file=sys.stderr)
        return 2

    try:
        ledger = read_ledger(Path(ledger_path))
        verdict = verify_report(document, ledger=ledger, public_key_hex=public_key)
    except ComplianceError as exc:
        print(f"compliance verify: {exc}", file=sys.stderr)
        return 2

    width = max(len(c.name) for c in verdict.checks)
    for check in verdict.checks:
        marker = {"ok": "OK  ", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}[check.status]
        print(f"{check.name.ljust(width)}  {marker}  {check.detail}")
    if verdict.findings:
        print()
        for finding in verdict.findings:
            label = "BREACH " if finding.severity == "breach" else "signal "
            print(f"{label} {finding.summary}")
    print()
    print(f"verdict: {verdict.verdict}")

    if append and verdict.verdict != "CONTRADICTED":
        entry = entry_from_report(
            document,
            public_key=verdict.public_key,
            content_sha256=verdict.content_sha256,
        )
        try:
            signer = _load_ledger_signer(ledger_key, ledger_key_id)
            written = append_entry(Path(ledger_path), entry, signer=signer)
        except ComplianceError as exc:
            print(f"compliance verify: {exc}", file=sys.stderr)
            return 2
        how = f"signed by {written.ledger_key_id}" if written.is_signed else "UNSIGNED"
        print(
            f"recorded report #{written.counter} for {written.install_id} in {ledger_path} ({how})"
        )
    elif append:
        print(
            "not recorded: a contradicted report must not become the new baseline",
            file=sys.stderr,
        )

    return 0 if verdict.verdict == "CONSISTENT" else 1


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _dispatch_audit(parsed: argparse.Namespace) -> int:
    if parsed.audit_command != "verify":
        print("Usage: matofx audit verify [--start-id N] [--end-id N]", file=sys.stderr)
        return 2
    return asyncio.run(_audit_verify(parsed.start_id, parsed.end_id))


async def _audit_verify(start_id: int | None, end_id: int | None) -> int:
    # Lazy imports keep the CLI startup snappy for `--version` etc.
    from matofx.audit import verify  # noqa: PLC0415
    from matofx.audit.keys import collect_public_keys  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415

    settings = load_settings()

    # Include retired keys so records signed before a rotation still verify.
    public_keys = (
        collect_public_keys(settings.audit.signing_key_path)
        if settings.audit.signing_key_path is not None
        else {}
    )

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            result = await verify(
                session,
                public_keys=public_keys or None,
                start_id=start_id,
                end_id=end_id,
            )

    if result.ok:
        print(f"audit verify: OK ({result.records_checked} records)")
        return 0

    print(
        f"audit verify: FAILED ({result.records_checked} records, {len(result.failures)} failures)",
        file=sys.stderr,
    )
    for failure in result.failures:
        print(f"  record {failure.record_id}: {failure.reason}", file=sys.stderr)
    return 1


def _dispatch_admin(parsed: argparse.Namespace) -> int:
    if parsed.admin_command == "backfill-validity":
        return asyncio.run(_admin_backfill_validity())
    if parsed.admin_command != "create-first-user":
        print(
            "Usage: matofx admin {create-first-user|backfill-validity} ...",
            file=sys.stderr,
        )
        return 2

    password = parsed.password
    if password is None:
        if sys.stdin.isatty():
            import getpass  # noqa: PLC0415

            password = getpass.getpass("Password: ")
        else:
            password = sys.stdin.readline().rstrip("\r\n")

    return asyncio.run(
        _admin_create_first_user(
            username=parsed.username,
            email=parsed.email,
            first_name=parsed.first_name,
            last_name=parsed.last_name,
            password=password,
        )
    )


async def _admin_create_first_user(
    *, username: str, email: str, first_name: str, last_name: str, password: str
) -> int:
    from matofx.audit import make_audit_writer  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.users import UserError, make_user_service  # noqa: PLC0415
    from matofx.users.bootstrap import BootstrapError, create_first_user  # noqa: PLC0415

    settings = load_settings()
    audit = make_audit_writer(settings.audit)
    user_service = make_user_service(settings.auth, audit)

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            try:
                user = await create_first_user(
                    session,
                    user_service,
                    username=username,
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                )
            except (BootstrapError, UserError) as exc:
                print(f"create-first-user: {exc}", file=sys.stderr)
                return 1
        print(f"Created super-user '{user.username}' (id={user.id}).")
    return 0


async def _admin_backfill_validity() -> int:
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.studies.results_service import backfill_validity  # noqa: PLC0415

    settings = load_settings()
    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            scanned, updated = await backfill_validity(session)
    print(f"backfill-validity: scanned {scanned} result(s), updated {updated}.")
    return 0


def _dispatch_migrate(parsed: argparse.Namespace) -> int:
    """Route ``matofx migrate ...`` to one of the per-subcommand runners."""
    cmd = parsed.migrate_command
    if cmd is None:
        print(
            "Usage: matofx migrate {fingerprint|trust|export|import} ...",
            file=sys.stderr,
        )
        return 2

    if cmd == "fingerprint":
        return asyncio.run(_migrate_fingerprint())
    if cmd == "trust":
        return _dispatch_migrate_trust(parsed)
    if cmd == "export":
        return asyncio.run(
            _migrate_export(
                tools=parsed.tools,
                workflows=parsed.workflows,
                notes=parsed.notes,
                out_path=parsed.out,
                acting_username=parsed.user,
                include_required=parsed.include_required,
            )
        )
    if cmd == "import":
        return asyncio.run(
            _migrate_import(
                bundle_path=parsed.bundle,
                dry_run=parsed.dry_run,
                acting_username=parsed.user,
            )
        )

    print(f"Unknown migrate subcommand: {cmd}", file=sys.stderr)
    return 2


def _dispatch_migrate_trust(parsed: argparse.Namespace) -> int:
    """Route ``matofx migrate trust ...`` to the per-action runner."""
    sub = parsed.trust_command
    if sub is None:
        print("Usage: matofx migrate trust {list|add|remove} ...", file=sys.stderr)
        return 2
    if sub == "list":
        return asyncio.run(_migrate_trust_list())
    if sub == "add":
        return asyncio.run(
            _migrate_trust_add(
                fingerprint=parsed.fingerprint,
                label=parsed.label,
                notes=parsed.notes,
                acting_username=parsed.user,
            )
        )
    if sub == "remove":
        return asyncio.run(
            _migrate_trust_remove(
                fingerprint=parsed.fingerprint,
                reason=parsed.reason,
                acting_username=parsed.user,
            )
        )
    print(f"Unknown trust subcommand: {sub}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# rotate signing-key
# ---------------------------------------------------------------------------


def _dispatch_rotate(parsed: argparse.Namespace) -> int:
    """Route ``matofx rotate ...`` to the per-key runner."""
    if parsed.rotate_command != "signing-key":
        print("Usage: matofx rotate signing-key [--init] [--path PATH]", file=sys.stderr)
        return 2
    return _rotate_signing_key(init=parsed.init, path_arg=parsed.path)


def _rotate_signing_key(*, init: bool, path_arg: str | None) -> int:
    """``matofx rotate signing-key`` — initialise or rotate the audit key.

    The target path comes from ``--path`` if given, otherwise from
    ``audit.signing_key_path`` in the config. Rotation retires the old
    public key so historical records stay verifiable; the operator must
    restart the services for the new key to take effect.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from matofx.audit.keys import AuditKeyError, rotate_signing_key  # noqa: PLC0415

    if path_arg is not None:
        target: _Path | None = _Path(path_arg)
    else:
        from matofx.config import load_settings  # noqa: PLC0415

        target = load_settings().audit.signing_key_path
        if target is None:
            print(
                "rotate signing-key: no key path known. Either set "
                "audit.signing_key_path in matofx.toml, or pass --path <file>.",
                file=sys.stderr,
            )
            return 2

    assert target is not None  # noqa: S101 - the None branch returned above
    try:
        result = rotate_signing_key(target, init=init)
    except AuditKeyError as exc:
        print(f"rotate signing-key: {exc}", file=sys.stderr)
        return 1

    if result.old_key_id is None:
        print(f"Initialised audit signing key at {target}")
        print(f"  key id: {result.new_key_id}")
        print(
            "Point audit.signing_key_path in matofx.toml at this file (if it "
            "isn't already) and restart the services so records are signed."
        )
    else:
        print(f"Rotated audit signing key at {target}")
        print(f"  old key id: {result.old_key_id} (retired — kept to verify historical records)")
        print(f"  new key id: {result.new_key_id}")
        print(f"  retired public key:  {result.retired_public_key_path}")
        print(f"  archived old key:    {result.archived_private_key_path}")
        print(
            "Restart the services so new records sign with the new key. Keep "
            "the retired-keys folder in your backups — `matofx audit verify` "
            "needs it to check records signed before the rotation."
        )
    return 0


# ---------------------------------------------------------------------------
# Migrate runner helpers
# ---------------------------------------------------------------------------


def _format_event(event: object) -> str:
    """Render one :class:`ProgressEvent` as a single stdout line.

    Prefixes by level keep the output greppable from shell scripts
    without losing the plain-English message the operator reads.
    """
    level = getattr(event, "level", "info")
    msg = getattr(event, "message", str(event))
    prefix = {
        "info": "  ",
        "imported": "OK   ",
        "skipped": "SKIP ",
        "warning": "WARN ",
        "error": "ERR  ",
        "summary": "---  ",
    }.get(level, "  ")
    return f"{prefix}{msg}"


async def _resolve_user_id(session: AsyncSession, username: str) -> int:
    """Look up the acting user's id by username, or raise SystemExit.

    The CLI needs an actor for every audit-emitting operation; we
    refuse early with a clear message rather than blow up mid-flow.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.users.orm import User  # noqa: PLC0415

    row = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if row is None:
        print(
            f"migrate: no user with username {username!r} on this install. "
            "Create the user first (Admin -> Users -> New user) and retry.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return int(row.id)


def _parse_tool_specs(raw: str) -> list[tuple[str, str]]:
    """Parse ``--tools alias:ver,alias:ver`` into ``[(alias, ver), ...]``.

    Empty strings are tolerated (returns ``[]``) so ``--tools ''`` is a
    legitimate workflow-only export.
    """
    out: list[tuple[str, str]] = []
    for raw_entry in (raw or "").split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            print(
                f"migrate export: tool spec {entry!r} must be in 'alias:version' form.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        alias, _, version = entry.partition(":")
        out.append((alias.strip(), version.strip()))
    return out


def _parse_alias_list(raw: str) -> list[str]:
    """Parse a comma-separated alias list. Empty entries dropped."""
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


async def _migrate_fingerprint() -> int:
    """``matofx migrate fingerprint`` — print the display form for paste-in."""
    from matofx.audit.keys import load_signing_key  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.migrate.signing import (  # noqa: PLC0415
        compute_fingerprint,
        display_fingerprint,
    )

    settings = load_settings()
    if settings.audit.signing_key_path is None:
        print(
            "migrate: this install has no audit signing key configured. "
            "Configure 'audit.signing_key_path' in matofx.toml or run "
            "`matofx rotate signing-key --init`.",
            file=sys.stderr,
        )
        return 1

    private_key = load_signing_key(settings.audit.signing_key_path)
    fp = compute_fingerprint(private_key.public_key())
    print(display_fingerprint(fp))
    print(f"(canonical: {fp})")
    return 0


async def _migrate_trust_list() -> int:
    """``matofx migrate trust list`` — print every trust entry."""
    from matofx.audit import make_audit_writer  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.migrate import make_trusted_peer_service  # noqa: PLC0415
    from matofx.migrate.signing import display_fingerprint  # noqa: PLC0415

    settings = load_settings()
    audit = make_audit_writer(settings.audit)
    service = make_trusted_peer_service(audit)

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            rows = await service.list_all(session)

    if not rows:
        print("(no trusted peers configured)")
        return 0
    print(f"{'LABEL':<24}  FINGERPRINT")
    for row in rows:
        print(f"{row.label:<24}  {display_fingerprint(row.fingerprint)}")
        if row.notes:
            print(f"{'':<24}  notes: {row.notes}")
        added = row.added_at.isoformat() if row.added_at else "?"
        last = row.last_used_at.isoformat() if row.last_used_at else "never used"
        print(f"{'':<24}  added: {added}  last used: {last}")
    return 0


async def _migrate_trust_add(
    *, fingerprint: str, label: str, notes: str, acting_username: str
) -> int:
    """``matofx migrate trust add`` — add a fingerprint with audit."""
    from matofx.audit import ensure_chain_head, make_audit_writer  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.migrate import make_trusted_peer_service  # noqa: PLC0415
    from matofx.migrate.errors import MigrateError  # noqa: PLC0415
    from matofx.migrate.signing import BundleSigningError, display_fingerprint  # noqa: PLC0415

    settings = load_settings()
    audit = make_audit_writer(settings.audit)
    service = make_trusted_peer_service(audit)

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            user_id = await _resolve_user_id(session, acting_username)
            try:
                row = await service.add_peer(
                    session,
                    actor_user_id=user_id,
                    fingerprint=fingerprint,
                    label=label,
                    notes=notes,
                )
            except (MigrateError, BundleSigningError, ValueError) as exc:
                print(f"migrate trust add: {exc}", file=sys.stderr)
                return 1
    print(f"Added trusted peer {label!r}: {display_fingerprint(row.fingerprint)}")
    return 0


async def _migrate_trust_remove(*, fingerprint: str, reason: str, acting_username: str) -> int:
    """``matofx migrate trust remove`` — remove a fingerprint with reason audit."""
    from matofx.audit import ensure_chain_head, make_audit_writer  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.migrate import make_trusted_peer_service  # noqa: PLC0415
    from matofx.migrate.errors import MigrateError  # noqa: PLC0415
    from matofx.migrate.signing import BundleSigningError  # noqa: PLC0415

    settings = load_settings()
    audit = make_audit_writer(settings.audit)
    service = make_trusted_peer_service(audit)

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            user_id = await _resolve_user_id(session, acting_username)
            try:
                await service.remove_peer(
                    session,
                    actor_user_id=user_id,
                    fingerprint=fingerprint,
                    reason=reason,
                )
            except (MigrateError, BundleSigningError) as exc:
                print(f"migrate trust remove: {exc}", file=sys.stderr)
                return 1
    print("Removed trusted peer.")
    return 0


async def _migrate_export(
    *,
    tools: str,
    workflows: str,
    notes: str,
    out_path: str,
    acting_username: str,
    include_required: bool = False,
) -> int:
    """``matofx migrate export`` — build and write a signed bundle."""
    from pathlib import Path as _Path  # noqa: PLC0415

    from matofx.audit import ensure_chain_head, make_audit_writer  # noqa: PLC0415
    from matofx.audit.keys import load_signing_key  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.migrate import make_export_service  # noqa: PLC0415
    from matofx.migrate.errors import MigrateError  # noqa: PLC0415
    from matofx.migrate.signing import KeyPair  # noqa: PLC0415

    settings = load_settings()
    if settings.audit.signing_key_path is None:
        print(
            "migrate export: this install has no signing key configured. "
            "Configure 'audit.signing_key_path' in matofx.toml.",
            file=sys.stderr,
        )
        return 1

    tool_specs = _parse_tool_specs(tools)
    workflow_aliases = _parse_alias_list(workflows)
    if not tool_specs and not workflow_aliases:
        print(
            "migrate export: nothing to bundle — pass at least one --tools "
            "alias:version or --workflows alias.",
            file=sys.stderr,
        )
        return 2

    private_key = load_signing_key(settings.audit.signing_key_path)
    key_pair = KeyPair.from_private_key(private_key)

    audit = make_audit_writer(settings.audit)
    service = make_export_service(audit, workflow_icons_dir=settings.workflows.icons_dir)

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            user_id = await _resolve_user_id(session, acting_username)
            try:
                selection = await service.resolve_selection(
                    session,
                    tool_specs=tool_specs,
                    workflow_aliases=workflow_aliases,
                    include_required=include_required,
                )
                for alias, version in selection.added:
                    print(f"  + including required tool {alias} {version}")
                bundle_bytes = await service.build_bundle(
                    session,
                    actor_user_id=user_id,
                    key_pair=key_pair,
                    source_install_id="cli",
                    tool_specs=selection.tool_specs,
                    workflow_aliases=workflow_aliases,
                    notes=notes,
                    actor_label=acting_username,
                    on_event=lambda e: print(_format_event(e)),
                )
            except MigrateError as exc:
                print(f"migrate export: {exc}", file=sys.stderr)
                return 1

    out = _Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bundle_bytes)
    print(f"Wrote {len(bundle_bytes):,} bytes to {out}")
    return 0


async def _migrate_import(*, bundle_path: str, dry_run: bool, acting_username: str) -> int:
    """``matofx migrate import`` — verify, trust-check, and import a bundle."""
    from pathlib import Path as _Path  # noqa: PLC0415

    from matofx.audit import ensure_chain_head, make_audit_writer  # noqa: PLC0415
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.db import make_engine_scope, make_session_factory, session_scope  # noqa: PLC0415
    from matofx.migrate import (  # noqa: PLC0415
        make_import_service,
        make_trusted_peer_service,
    )
    from matofx.migrate.bundle import BundleError  # noqa: PLC0415
    from matofx.migrate.errors import MigrateError  # noqa: PLC0415
    from matofx.migrate.signing import BundleSigningError  # noqa: PLC0415

    src = _Path(bundle_path)
    if not src.is_file():
        print(f"migrate import: no such file: {src}", file=sys.stderr)
        return 1
    bundle_bytes = src.read_bytes()
    if not bundle_bytes:
        print(f"migrate import: {src} is empty.", file=sys.stderr)
        return 1

    settings = load_settings()
    audit = make_audit_writer(settings.audit)
    trust_service = make_trusted_peer_service(audit)
    import_service = make_import_service(
        audit,
        trust_service=trust_service,
        bundle_root=settings.tools.bundles_dir,
        workflow_icons_dir=settings.workflows.icons_dir,
    )

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            user_id = await _resolve_user_id(session, acting_username)
            try:
                result = await import_service.import_bundle(
                    session,
                    actor_user_id=user_id,
                    bundle_bytes=bundle_bytes,
                    dry_run=dry_run,
                    on_event=lambda e: print(_format_event(e)),
                )
            except (MigrateError, BundleError, BundleSigningError) as exc:
                print(f"migrate import: {exc}", file=sys.stderr)
                return 1

    mode = "DRY-RUN" if dry_run else "DONE"
    print(
        f"---  [{mode}] {result.imported_count} imported, "
        f"{result.skipped_count} skipped, "
        f"{result.noop_count} already present."
    )
    # Non-zero exit when nothing imported AND something was skipped — lets
    # CI pipelines treat partial bundles as actionable failures.
    if result.imported_count == 0 and result.skipped_count > 0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# secrets store management
# ---------------------------------------------------------------------------


def _dispatch_secrets(parsed: argparse.Namespace) -> int:
    """Route ``matofx secrets ...`` to the per-action handler."""
    cmd = parsed.secrets_command
    if cmd == "init":
        return _secrets_init()
    if cmd == "set":
        return _secrets_set(ref=parsed.ref, value=parsed.value)
    if cmd == "get":
        return asyncio.run(_secrets_get(parsed.ref))
    if cmd == "list":
        return _secrets_list()
    if cmd == "remove":
        return _secrets_remove(parsed.ref)
    print("Usage: matofx secrets {init|set|get|list|remove} ...", file=sys.stderr)
    return 2


def _secrets_init() -> int:
    """``matofx secrets init`` — print a fresh master key (stdout) + guidance (stderr)."""
    from matofx.secrets import generate_master_key  # noqa: PLC0415

    print(generate_master_key())
    print(
        "\nThe key above is your secrets master key. Add it to matofx.toml:\n"
        "  [secrets]\n"
        '  backend = "encrypted_file"\n'
        '  file_path = "secrets.json"\n'
        '  master_key = "<the key above>"\n'
        "(or set MATOFX__SECRETS__MASTER_KEY). Store it safely and back it up — "
        "without it every stored secret is unrecoverable.",
        file=sys.stderr,
    )
    return 0


def _open_encrypted_store_or_report() -> object | None:
    """Open the configured encrypted store, or print why it can't and return None."""
    from matofx.config import load_settings  # noqa: PLC0415
    from matofx.secrets import SecretsConfigError, open_encrypted_store  # noqa: PLC0415

    settings = load_settings()
    try:
        return open_encrypted_store(settings.secrets)
    except SecretsConfigError as exc:
        print(f"matofx secrets: {exc}", file=sys.stderr)
        return None


def _secrets_set(*, ref: str, value: str | None) -> int:
    """``matofx secrets set`` — encrypt + store a secret under ``ref``."""
    store = _open_encrypted_store_or_report()
    if store is None:
        return 1
    if value is None:
        if sys.stdin.isatty():
            import getpass  # noqa: PLC0415

            value = getpass.getpass(f"Secret for {ref!r}: ")
        else:
            value = sys.stdin.read().rstrip("\r\n")
    store.set_secret(ref, value)  # type: ignore[attr-defined]
    print(f"Stored secret for reference {ref!r}.")
    return 0


async def _secrets_get(ref: str) -> int:
    """``matofx secrets get`` — write the raw secret material to stdout."""
    from matofx.secrets import SecretNotFoundError, SecretsDecryptError  # noqa: PLC0415

    store = _open_encrypted_store_or_report()
    if store is None:
        return 1
    try:
        secret = await store.resolve(ref)  # type: ignore[attr-defined]
    except SecretNotFoundError:
        print(f"matofx secrets get: no secret for reference {ref!r}.", file=sys.stderr)
        return 1
    except SecretsDecryptError as exc:
        print(f"matofx secrets get: {exc}", file=sys.stderr)
        return 1
    # Raw bytes, no trailing newline, so the material round-trips exactly
    # (passwords have no newline; key files keep their own).
    sys.stdout.buffer.write(secret.as_bytes())
    return 0


def _secrets_list() -> int:
    """``matofx secrets list`` — print stored references (non-secret)."""
    store = _open_encrypted_store_or_report()
    if store is None:
        return 1
    refs = store.list_refs()  # type: ignore[attr-defined]
    if not refs:
        print("(no secrets stored)")
        return 0
    for ref in refs:
        print(ref)
    return 0


def _secrets_remove(ref: str) -> int:
    """``matofx secrets remove`` — delete a secret by reference."""
    store = _open_encrypted_store_or_report()
    if store is None:
        return 1
    if not store.remove_secret(ref):  # type: ignore[attr-defined]
        print(f"matofx secrets remove: no secret for reference {ref!r}.", file=sys.stderr)
        return 1
    print(f"Removed secret for reference {ref!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
