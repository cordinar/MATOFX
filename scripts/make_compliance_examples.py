"""Generate the worked compliance-report examples in ``designs/examples/``.

These are **real artefacts**, not mock-ups: produced by
:func:`matofx.compliance.generate_report` against a migrated database and
checked by :func:`matofx.compliance.verify_report`. They double as the
reference fixtures for ADR-0019 slice 1, so anyone reimplementing the
format (or the Funis-side verifier) has valid vectors to test against.

Run from the repo root::

    matofx/.venv/Scripts/python.exe scripts/make_compliance_examples.py

Produces, for a fictional customer:

- ``funis-install-identity.json``     the out-of-band issued identity (slice 1)
- ``matofx-licence.json``            the signed CSL licence (slice 2)
- ``compliance-report-1.json/.html``  first report — establishes the baseline
- ``compliance-report-2.json/.html``  honest follow-up, CSL licence, over cap
- ``compliance-report-3-tampered.json`` a rebuilt chain that self-verifies
- ``funis-heads.jsonl``               the vendor-side ledger after report 2
- ``verify-transcripts.txt``          what ``matofx compliance verify`` prints
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "matofx" / "src"))
sys.path.insert(0, str(REPO_ROOT / "matofx"))

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from matofx.audit import (  # noqa: E402
    RecordInput,
    ensure_chain_head,
    generate_signing_key,
    make_audit_writer,
)
from matofx.audit.keys import compute_key_id  # noqa: E402
from matofx.audit.orm import AuditLog  # noqa: E402
from matofx.compliance import (  # noqa: E402
    adopt_identity,
    append_entry,
    entry_from_report,
    generate_report,
    load_identity_file,
    read_ledger,
    render_report_html,
    verify_report,
)
from matofx.compliance.canonical import canonical_bytes  # noqa: E402
from matofx.config import (  # noqa: E402
    AuditSettings,
    ComplianceSettings,
    DbSettings,
    LicenceSettings,
    Settings,
)
from matofx.licence import install_licence, licence_snapshot, resolve_licence  # noqa: E402
from matofx.db import make_engine, make_session_factory  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from matofx.users.orm import AuthSession, User  # noqa: E402
from matofx.users.role_orm import Role  # noqa: E402

OUT = REPO_ROOT / "designs" / "examples"
CUSTOMER = "Northwind Aerospace Ltd"
INSTALL_ID = "inst_7Q2M4KJ9"
ISSUER_KEY_ID = "funis-2026-01"

# Fixed seeds so re-running produces the same keys (the artefacts stay
# diff-able). These are EXAMPLE keys in a public design folder — they are
# not, and must never become, real issuing material.
ISSUER_SEED = "11" * 32
REPORT_SEED = "22" * 32


def _alembic_upgrade(url: str) -> None:
    cfg = Config(str(REPO_ROOT / "matofx" / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(REPO_ROOT / "matofx" / "src" / "matofx" / "db" / "migrations")
    )
    cfg.cmd_opts = type("Args", (), {"x": [f"url={url}"]})()
    command.upgrade(cfg, "head")


def _write_identity(path: Path) -> None:
    issuer = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(ISSUER_SEED))
    report_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(REPORT_SEED))
    identity = {
        "install_id": INSTALL_ID,
        "customer": CUSTOMER,
        "environment": "production",
        "issued_at": "2026-01-14T00:00:00Z",
        "report_key_seed": REPORT_SEED,
        "report_key_id": compute_key_id(report_key.public_key()),
    }
    document = {
        "identity": identity,
        "issuer": {
            "key_id": ISSUER_KEY_ID,
            "alg": "ed25519",
            "signature": issuer.sign(canonical_bytes({"identity": identity})).hex(),
        },
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _write_licence(path: Path) -> None:
    """A signed CSL licence carrying the same report key as the identity."""
    issuer = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(ISSUER_SEED))
    report_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(REPORT_SEED))
    payload = {
        "schema_version": 1,
        "licence_id": "LIC-2026-0043",
        "customer": CUSTOMER,
        "environment": "production",
        "install_id": INSTALL_ID,
        "licence_type": "csl",
        "n_slots": 10,
        "issued_at": "2026-01-14T00:00:00Z",
        "expires_at": "2027-01-14T00:00:00Z",
        "grace_period_days": 30,
        "features": {
            "parametric_studies": True,
            "optimisation_framework": True,
            "vv_tracking": True,
        },
        "machine_binding": None,
        "banner_text": None,
        "report_key_seed": REPORT_SEED,
        "report_key_id": compute_key_id(report_key.public_key()),
    }
    document = {
        "licence": payload,
        "issuer": {
            "key_id": ISSUER_KEY_ID,
            "alg": "ed25519",
            "signature": issuer.sign(canonical_bytes({"licence": payload})).hex(),
        },
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _issuer_public_hex() -> str:
    issuer = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(ISSUER_SEED))
    return issuer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()


async def _seed_records(
    session: AsyncSession, settings: Settings, count: int, action: str = "login.success"
) -> None:
    writer = make_audit_writer(settings.audit)
    for index in range(count):
        await writer.record(
            session,
            RecordInput(
                actor=f"user:{index % 4 + 1}",
                actor_type="user",
                action=action,
                resource_type="session",
                resource_id=f"s-{index}",
                outcome="success",
            ),
        )
    await session.flush()


async def _seed_people(session: AsyncSession, count: int) -> None:
    role_id = (
        await session.execute(sa.select(Role.id).where(Role.name == "regular_user"))
    ).scalar_one()
    start = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=3)
    for index in range(count):
        user = User(
            username=f"engineer{index}",
            email=f"engineer{index}@northwind.example",
            first_name="Engineer",
            last_name=str(index),
            full_name=f"Engineer {index}",
            password_hash="not-a-real-hash",  # noqa: S106 - example fixture only
            is_active=True,
            role_id=role_id,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=f"hash-{index}",
                created_at=start,
                last_activity_at=_dt.datetime.now(tz=_dt.UTC),
            )
        )
    await session.flush()


def _transcript(title: str, document: dict, ledger_path: Path, public_key: str | None) -> str:
    """Render what ``matofx compliance verify`` prints for a document."""
    verdict = verify_report(
        document, ledger=read_ledger(ledger_path), public_key_hex=public_key
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
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
    return f"$ {title}\n\n{buffer.getvalue()}"


async def _tampered_report(
    session: AsyncSession,
    *,
    settings: Settings,
    issued: object,
    licence: object,
    ledger_path: Path,
) -> str:
    """Rewrite the attested history, then report honestly on top of it."""
    # --- report 3: the chain is rebuilt between reports -------------
    attested = read_ledger(ledger_path)[-1]
    await session.execute(
        sa.update(AuditLog).where(AuditLog.id == attested.head_id).values(hash="6b" * 32)
    )
    await session.execute(sa.update(AuditLog).where(AuditLog.id == 1).values(hash="91" * 32))
    await session.commit()
    await _seed_records(session, settings, 30)
    await session.commit()
    third = await generate_report(
        session, settings=settings, issued=issued, actor="user:1", licence=licence
    )
    await session.commit()
    _dump(third.document, OUT / "compliance-report-3-tampered.json")
    return _transcript(
        "matofx compliance verify compliance-report-3-tampered.json --ledger funis-heads.jsonl",
        third.document,
        ledger_path,
        None,
    )


async def _build(workdir: Path, db_path: Path) -> None:
    identity_path = OUT / "funis-install-identity.json"
    db_settings = DbSettings.from_url(f"sqlite:///{db_path}", dialect="sqlite")

    signing_key = workdir / "audit-signing.key"
    generate_signing_key(signing_key)
    settings = Settings(
        environment="test",
        db=db_settings,
        audit=AuditSettings(signing_key_path=signing_key, require_signing=True),
        compliance=ComplianceSettings(
            identity_file=identity_path,
            issuer_public_keys={ISSUER_KEY_ID: _issuer_public_hex()},
        ),
    )
    issued = load_identity_file(
        identity_path, extra_issuer_keys={ISSUER_KEY_ID: _issuer_public_hex()}
    )
    public_key = issued.public_key_hex()
    ledger_path = OUT / "funis-heads.jsonl"
    ledger_path.unlink(missing_ok=True)

    engine = make_engine(db_settings)
    factory = make_session_factory(engine)
    transcripts: list[str] = []

    async with factory() as session:
        await ensure_chain_head(session)
        await _seed_records(session, settings, 120)
        await _seed_people(session, 11)
        await adopt_identity(session, issued, now=_dt.datetime.now(tz=_dt.UTC))
        await session.commit()

        # --- report 1: baseline, before any licence is installed --------
        first = await generate_report(
            session, settings=settings, issued=issued, actor="user:1"
        )
        await session.commit()
        _dump(first.document, OUT / "compliance-report-1.json")
        (OUT / "compliance-report-1.html").write_text(
            render_report_html(first.document), encoding="utf-8"
        )
        transcripts.append(
            _transcript(
                "matofx compliance verify compliance-report-1.json "
                f"--ledger funis-heads.jsonl --public-key {public_key[:16]}…",
                first.document,
                ledger_path,
                public_key,
            )
        )
        append_entry(
            ledger_path,
            entry_from_report(
                first.document, public_key=public_key, content_sha256=first.content_sha256
            ),
        )

        # --- report 2: a month later, CSL licence, over cap -------------
        await _seed_records(session, settings, 340)
        await _seed_records(session, settings, 1, action="licence.slot.breakglass")
        # The month's work: every session stays active inside the new
        # reporting window, so peak concurrency (11) exceeds the 10 slots
        # and the break-glass admission accounts for the excess.
        await session.execute(
            sa.update(AuthSession).values(last_activity_at=_dt.datetime.now(tz=_dt.UTC))
        )
        await session.commit()
        # Slice 2: a real signed licence, verified through the licence
        # subsystem, rather than a hand-built snapshot.
        licence_path = OUT / "matofx-licence.json"
        _write_licence(licence_path)
        licensed = settings.model_copy(
            update={"licence": LicenceSettings(file=licence_path)}
        )
        resolution = resolve_licence(licensed)
        await install_licence(
            session, settings=licensed, loaded=resolution.loaded, actor="user:1"
        )
        await session.commit()
        licence = licence_snapshot(resolution)
        second = await generate_report(
            session, settings=licensed, issued=issued, actor="user:1", licence=licence
        )
        await session.commit()
        _dump(second.document, OUT / "compliance-report-2.json")
        (OUT / "compliance-report-2.html").write_text(
            render_report_html(second.document), encoding="utf-8"
        )
        transcripts.append(
            _transcript(
                "matofx compliance verify compliance-report-2.json --ledger funis-heads.jsonl",
                second.document,
                ledger_path,
                None,
            )
        )
        append_entry(
            ledger_path,
            entry_from_report(
                second.document, public_key=public_key, content_sha256=second.content_sha256
            ),
        )

        transcripts.append(
            await _tampered_report(
                session,
                settings=licensed,
                issued=issued,
                licence=licence,
                ledger_path=ledger_path,
            )
        )

    await engine.dispose()
    (OUT / "verify-transcripts.txt").write_text(
        "\n\n".join(transcripts).replace("\r\n", "\n"), encoding="utf-8"
    )
    print(f"wrote {len(list(OUT.iterdir()))} files to {OUT}")


def _dump(document: dict, path: Path) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """Build every example artefact into ``designs/examples/``."""
    workdir = Path(tempfile.mkdtemp(prefix="matofx-compliance-examples-"))
    db_path = workdir / "example.db"
    OUT.mkdir(parents=True, exist_ok=True)
    _write_identity(OUT / "funis-install-identity.json")
    # Alembic drives its own event loop, so migrate before asyncio.run().
    _alembic_upgrade(f"sqlite:///{db_path}")
    try:
        asyncio.run(_build(workdir, db_path))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
