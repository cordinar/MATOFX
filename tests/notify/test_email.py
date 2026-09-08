"""Tests for :class:`EmailNotifier` and :func:`make_notifier`.

We don't stand a real SMTP server up. Instead we monkey-patch
``aiosmtplib.send`` and capture every call — that exercises the
notifier's full flow (settings resolution, recipient lookup,
descriptor flag, rendering, audit on success / failure / skip)
without binding a port or sending real mail.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.config import AuthSettings, EmailSettings
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.notify import EmailNotifier, NoopNotifier, make_notifier
from matofx.tools.exec_orm import StorageLocation
from matofx.tools.loader import install_from_path
from matofx.tools.orm import Tool, ToolVersion
from matofx.tools.service import make_tool_service
from matofx.users import make_password_hasher, make_user_service
from tests.notify.conftest import patch_smtp

# --- helpers ---------------------------------------------------------


async def _seed(api_engine: AsyncEngine, tmp_path: Path) -> dict[str, Any]:
    """Minimal seed: super-user + storage_location + installed tool + a running job."""
    factory = make_session_factory(api_engine)
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    tool_service = make_tool_service(audit)
    repo_root = Path(__file__).resolve().parents[2]

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="testpassword123",
            full_name="Alice Tester",
            role="super_user",
        )
        mount = tmp_path / "mount"
        mount.mkdir()
        location = StorageLocation(name="local", mount_path=str(mount))
        session.add(location)
        await session.flush()
        installed = await install_from_path(
            session,
            tool_service,
            actor_user_id=user.id,
            bundle_dir=repo_root / "example_tools" / "material_properties",
        )
        await tool_service.promote_state(
            session, actor_user_id=user.id, alias=installed.alias, new_state="test"
        )
        await tool_service.promote_state(
            session, actor_user_id=user.id, alias=installed.alias, new_state="released"
        )
        # Job-state emails are opt-in per tool, and the example bundle
        # doesn't ask for them — so the seeded tool has to say yes for
        # the send-path tests below to have anything to assert. The
        # opt-out test flips this back to False explicitly.
        installed_tool = (
            await session.execute(sa.select(Tool).where(Tool.alias == installed.alias))
        ).scalar_one()
        installed_tool.descriptor = {
            **dict(installed_tool.descriptor or {}),
            "email_notifications": True,
        }
        project = Project(name="p", description="", creator_user_id=user.id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        session.add(scenario)
        await session.flush()
        # install_from_path → tool_service.install creates an initial
        # ToolVersion snapshot; subsequent promotions snap to 1.0.0,
        # which adds another. Either way, jobs pin to the latest
        # snapshot for the alias (NOT NULL FK from Session B).
        latest_version_id = (
            await session.execute(
                sa.select(ToolVersion.id)
                .where(ToolVersion.alias == installed.alias)
                .order_by(ToolVersion.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        # Leave the job in its default ``created`` state — the notifier
        # only reads identity / name fields, not state-dependent data,
        # so we side-step the started_at >= created_at CHECK constraint
        # by not pre-stamping started_at in the seed.
        job = Job(
            scenario_id=scenario.id,
            name="lifecycle",
            tool_alias=installed.alias,
            tool_version_id=int(latest_version_id),
            inputs={},
            created_by_user_id=user.id,
        )
        session.add(job)
        await session.flush()
        return {
            "factory": factory,
            "audit": audit,
            "user_id": user.id,
            "user_email": user.email,
            "tool_alias": installed.alias,
            "job_id": int(job.id),
        }


def _smtp_settings() -> EmailSettings:
    return EmailSettings(
        enabled=True,
        smtp_host="smtp.invalid",
        smtp_port=2525,
        from_address="matofx@example.com",
        public_url="https://matofx.example.com",
        use_tls=False,
        use_starttls=False,
    )


# --- tests -----------------------------------------------------------


def test_make_notifier_returns_noop_when_disabled() -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    notifier = make_notifier(EmailSettings(enabled=False), audit)
    assert isinstance(notifier, NoopNotifier)


def test_make_notifier_returns_email_when_enabled() -> None:
    audit = AuditWriter(signing_key=None, key_id=None)
    notifier = make_notifier(_smtp_settings(), audit)
    assert isinstance(notifier, EmailNotifier)


@pytest.mark.asyncio
async def test_email_notifier_sends_for_complete_event(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: aiosmtplib.send is called with the rendered message."""
    seed = await _seed(api_engine, tmp_path)
    captured: list[dict[str, Any]] = []

    async def _fake_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        captured.append({"to": msg["To"], "subject": msg["Subject"], "kwargs": kwargs})
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        job.exit_code = 0
        await notifier.notify_job_state_changed(session, job=job, event="complete")

    assert len(captured) == 1
    sent = captured[0]
    assert sent["to"] == seed["user_email"]
    assert "complete" in sent["subject"].lower()
    assert sent["kwargs"]["hostname"] == "smtp.invalid"
    assert sent["kwargs"]["port"] == 2525
    # Secure default: no custom TLS context — aiosmtplib builds its
    # own verifying default.
    assert "tls_context" not in sent["kwargs"]


@pytest.mark.asyncio
async def test_email_notifier_skips_cert_verification_when_opted_out(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tls_verify=False passes an insecure SSL context to aiosmtplib.

    The escape hatch for TLS-intercepting AV/proxies. Disables peer-
    cert + hostname checks for the SMTP TLS layer only; never the
    default.
    """
    seed = await _seed(api_engine, tmp_path)
    captured: list[dict[str, Any]] = []

    async def _fake_send(_msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        captured.append({"kwargs": kwargs})
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    settings = EmailSettings(
        enabled=True,
        smtp_host="smtp.invalid",
        smtp_port=2525,
        from_address="matofx@example.com",
        public_url="https://matofx.example.com",
        use_tls=False,
        use_starttls=True,
        tls_verify=False,
    )
    notifier = EmailNotifier(settings, seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        job.exit_code = 0
        await notifier.notify_job_state_changed(session, job=job, event="complete")

    assert len(captured) == 1
    ctx = captured[0]["kwargs"].get("tls_context")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_email_notifier_audits_success(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine, tmp_path)

    async def _fake_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        del msg, kwargs
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        await notifier.notify_job_state_changed(session, job=job, event="running")

    async with session_scope(seed["factory"]) as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "notify.email.sent")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].outcome == "success"


@pytest.mark.asyncio
async def test_email_notifier_audits_failure_on_smtp_error(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMTP raises → notifier audits notify.email.failed and does NOT propagate."""
    seed = await _seed(api_engine, tmp_path)

    async def _broken_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        del msg, kwargs
        raise OSError("smtp.invalid: connection refused")

    patch_smtp(monkeypatch, _broken_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        # MUST NOT raise — runner relies on this.
        await notifier.notify_job_state_changed(session, job=job, event="failed")

    async with session_scope(seed["factory"]) as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "notify.email.failed")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].outcome == "failure"


@pytest.mark.asyncio
async def test_email_notifier_skips_when_descriptor_opts_out(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tool.descriptor.email_notifications=False → no send, no audit row."""
    seed = await _seed(api_engine, tmp_path)

    # Patch the descriptor on the tool row to disable email notifications.
    async with session_scope(seed["factory"]) as session:
        tool = (
            await session.execute(sa.select(Tool).where(Tool.alias == seed["tool_alias"]))
        ).scalar_one()
        descriptor = dict(tool.descriptor or {})
        descriptor["email_notifications"] = False
        tool.descriptor = descriptor

    captured: list[Any] = []

    async def _fake_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        del kwargs
        captured.append(msg)
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        await notifier.notify_job_state_changed(session, job=job, event="complete")

    assert captured == []


@pytest.mark.asyncio
async def test_email_notifier_skips_when_descriptor_is_silent(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``email_notifications`` key at all → no send: the flag is opt-in.

    A hand-written or imported bundle that never mentions notifications
    must not start mailing submitters. "Not stated" reads as off, the
    same answer the descriptor schema's default gives.
    """
    seed = await _seed(api_engine, tmp_path)

    async with session_scope(seed["factory"]) as session:
        tool = (
            await session.execute(sa.select(Tool).where(Tool.alias == seed["tool_alias"]))
        ).scalar_one()
        descriptor = dict(tool.descriptor or {})
        descriptor.pop("email_notifications", None)
        tool.descriptor = descriptor

    captured: list[Any] = []

    async def _fake_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        del kwargs
        captured.append(msg)
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        await notifier.notify_job_state_changed(session, job=job, event="complete")

    assert captured == []


@pytest.mark.asyncio
async def test_email_notifier_skips_when_recipient_opts_out(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user.email_notifications_enabled=False → no send, audited as skipped."""
    seed = await _seed(api_engine, tmp_path)

    # Flip the recipient's per-user opt-in off.
    async with session_scope(seed["factory"]) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        user = await session.get(User, seed["user_id"])
        assert user is not None
        user.email_notifications_enabled = False

    captured: list[Any] = []

    async def _fake_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        del kwargs
        captured.append(msg)
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        await notifier.notify_job_state_changed(session, job=job, event="complete")

    assert captured == []
    async with session_scope(seed["factory"]) as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "notify.email.skipped")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        payload = rows[0].payload or {}
        assert payload.get("reason") == "recipient opted out"


@pytest.mark.asyncio
async def test_noop_notifier_never_sends(api_engine: AsyncEngine, tmp_path: Path) -> None:
    """NoopNotifier is a true no-op — no audit, no send, never raises."""
    seed = await _seed(api_engine, tmp_path)
    notifier = NoopNotifier()
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        await notifier.notify_job_state_changed(session, job=job, event="complete")

    async with session_scope(seed["factory"]) as session:
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action.like("notify.email.%"))
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_render_includes_deep_link(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered body carries the configured public_url + /ui/my-jobs?job={id}."""
    seed = await _seed(api_engine, tmp_path)
    captured: list[Any] = []

    async def _fake_send(msg: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        del kwargs
        captured.append(msg.get_content())
        return ({}, "")

    patch_smtp(monkeypatch, _fake_send)

    notifier = EmailNotifier(_smtp_settings(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        job = await session.get(Job, seed["job_id"])
        assert job is not None
        await notifier.notify_job_state_changed(session, job=job, event="running")

    assert len(captured) == 1
    body = str(captured[0])
    assert f"https://matofx.example.com/ui/my-jobs?job={seed['job_id']}" in body
