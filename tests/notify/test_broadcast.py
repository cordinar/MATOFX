"""Tests for :class:`matofx.notify.BroadcastService`.

The SMTP layer is faked by monkeypatching
``matofx.notify.broadcast.SmtpSession`` so we exercise audience
resolution, template rendering (text + inline image), the per-
recipient summary, and audit — without a real relay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.config import AuthSettings, EmailSettings
from matofx.config.general_service import make_general_settings_service
from matofx.db import make_session_factory, session_scope
from matofx.notify import BroadcastAttachmentInput, BroadcastService
from matofx.users import make_password_hasher, make_user_service
from matofx.users.role_orm import UserCapabilityRole
from matofx.users.team_orm import Team, UserTeam

_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _fake_smtp_factory(
    sent: list[Any],
    *,
    fail_for: frozenset[str] = frozenset(),
    connect_error: Exception | None = None,
) -> type:
    class _FakeSmtp:
        def __init__(self, effective: dict[str, Any]) -> None:
            self._effective = effective

        async def __aenter__(self) -> _FakeSmtp:
            if connect_error is not None:
                raise connect_error
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def send(self, message: Any) -> None:
            if message["To"] in fail_for:
                raise RuntimeError("mailbox unavailable")
            sent.append(message)

    return _FakeSmtp


async def _seed_users(api_engine: AsyncEngine) -> dict[str, Any]:
    factory = make_session_factory(api_engine)
    auth = AuthSettings(argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1)
    audit = AuditWriter(signing_key=None, key_id=None)
    svc = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        admin = await svc.create_user(
            session,
            username="admin",
            email="admin@example.com",
            password="testpassword123",
            full_name="Admin",
            role="super_user",
        )
        active = await svc.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="testpassword123",
            full_name="Bob",
            role="regular_user",
        )
        inactive = await svc.create_user(
            session,
            username="carol",
            email="carol@example.com",
            password="testpassword123",
            full_name="Carol",
            role="regular_user",
        )
        await session.execute(
            sa.text('UPDATE "user" SET is_active = :a WHERE id = :i').bindparams(
                a=False, i=inactive.id
            )
        )
    return {
        "factory": factory,
        "audit": audit,
        "admin_id": admin.id,
        "active_id": active.id,
        "inactive_id": inactive.id,
        "active_email": active.email,
        "inactive_email": inactive.email,
        "admin_email": admin.email,
    }


async def _role_id(session: Any, name: str) -> int:
    """Look up a seeded role's id by name."""
    return int(
        (
            await session.execute(sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name))
        ).scalar_one()
    )


def _service(seed: dict[str, Any], tmp_path: Path, *, enabled: bool = True) -> BroadcastService:
    return BroadcastService(
        audit=seed["audit"],
        settings=EmailSettings(
            enabled=enabled,
            smtp_host="smtp.invalid",
            from_address="matofx@example.com",
            use_tls=False,
            use_starttls=False,
        ),
        branding_dir=tmp_path / "branding",
    )


@pytest.mark.asyncio
async def test_broadcast_disabled_when_email_off(api_engine: AsyncEngine, tmp_path: Path) -> None:
    seed = await _seed_users(api_engine)
    svc = _service(seed, tmp_path, enabled=False)
    async with session_scope(seed["factory"]) as session:
        result = await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            subject="S",
            body="B",
            attachments=[],
        )
    assert result.disabled is True
    assert result.attempted == 0
    assert result.sent == 0


@pytest.mark.asyncio
async def test_broadcast_active_audience_excludes_inactive(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        result = await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="active",
            subject="Heads up",
            body="line1\nline2",
            attachments=[],
        )
    recipients = {m["To"] for m in sent}
    assert seed["inactive_email"] not in recipients
    assert seed["active_email"] in recipients
    assert result.sent == len(recipients)
    assert result.failed == []
    # HTML alternative present with the escaped body, plus a plain text part.
    body_html = sent[0].get_body(("html",)).get_content()
    assert "line1<br/>line2" in body_html


@pytest.mark.asyncio
async def test_broadcast_all_audience_includes_inactive(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            subject="S",
            body="B",
            attachments=[],
        )
    recipients = {m["To"] for m in sent}
    assert seed["inactive_email"] in recipients
    assert seed["active_email"] in recipients


@pytest.mark.asyncio
async def test_broadcast_partial_failure_summarised_and_audited(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr(
        "matofx.notify.broadcast.SmtpSession",
        _fake_smtp_factory(sent, fail_for=frozenset({seed["inactive_email"]})),
    )
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        result = await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            subject="S",
            body="B",
            attachments=[],
        )
        rows = (
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "notify.broadcast.sent")
                )
            )
            .scalars()
            .all()
        )
    assert result.sent >= 1
    assert [f["email"] for f in result.failed] == [seed["inactive_email"]]
    assert len(rows) == 1
    assert rows[0].outcome == "failure"


@pytest.mark.asyncio
async def test_broadcast_teams_audience_targets_members(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    async with session_scope(seed["factory"]) as session:
        team = Team(name="R&D", description="")
        session.add(team)
        await session.flush()
        team_id = team.id
        session.add(UserTeam(user_id=seed["active_id"], team_id=team_id))
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            group_kind="teams",
            subject="S",
            body="B",
            attachments=[],
            selection=[str(team_id)],
        )
    recipients = {m["To"] for m in sent}
    assert recipients == {seed["active_email"]}


@pytest.mark.asyncio
async def test_broadcast_roles_audience_targets_holders(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    async with session_scope(seed["factory"]) as session:
        regular_id = await _role_id(session, "regular_user")
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            group_kind="roles",
            subject="S",
            body="B",
            attachments=[],
            selection=[str(regular_id)],
        )
    recipients = {m["To"] for m in sent}
    # Both regular users (incl. the deactivated one) — never the super_user.
    assert recipients == {seed["active_email"], seed["inactive_email"]}


@pytest.mark.asyncio
async def test_broadcast_curators_audience_includes_capability_and_sentinel(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    async with session_scope(seed["factory"]) as session:
        vv_curator_id = await _role_id(session, "vv_curator")
        session.add(UserCapabilityRole(user_id=seed["active_id"], role_id=vv_curator_id))
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            group_kind="curators",
            subject="S",
            body="B",
            attachments=[],
            selection=["vv"],
        )
    recipients = {m["To"] for m in sent}
    # super_user holds vv.curate via the sentinel; bob via the capability role.
    assert seed["admin_email"] in recipients
    assert seed["active_email"] in recipients
    assert seed["inactive_email"] not in recipients


@pytest.mark.asyncio
async def test_broadcast_active_scope_intersects_group(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    async with session_scope(seed["factory"]) as session:
        regular_id = await _role_id(session, "regular_user")
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="active",
            group_kind="roles",
            subject="S",
            body="B",
            attachments=[],
            selection=[str(regular_id)],
        )
    recipients = {m["To"] for m in sent}
    # 'active' scope drops the deactivated regular user from the role group.
    assert recipients == {seed["active_email"]}


@pytest.mark.asyncio
async def test_broadcast_group_audience_empty_selection_sends_nothing(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        result = await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="all",
            group_kind="teams",
            subject="S",
            body="B",
            attachments=[],
            selection=[],
        )
    assert result.attempted == 0
    assert sent == []


@pytest.mark.asyncio
async def test_broadcast_inlines_template_image(
    api_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_users(api_engine)
    sent: list[Any] = []
    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _fake_smtp_factory(sent))
    branding = tmp_path / "branding"
    gss = make_general_settings_service(seed["audit"])
    async with session_scope(seed["factory"]) as session:
        await ensure_chain_head(session)
        await gss.set(
            session,
            actor_user_id=seed["admin_id"],
            actor_role="super_user",
            key="broadcast_header_text",
            value="Hello team",
        )
        await gss.set_image(
            session,
            actor_user_id=seed["admin_id"],
            actor_role="super_user",
            key="broadcast_header_image",
            filename="banner.png",
            data=_PNG_1PX,
            mime_type="image/png",
            branding_dir=branding,
            max_bytes=1_000_000,
        )
    svc = _service(seed, tmp_path)
    async with session_scope(seed["factory"]) as session:
        await svc.send_broadcast(
            session,
            actor_user_id=seed["admin_id"],
            audience="active",
            subject="S",
            body="Body copy",
            attachments=[
                BroadcastAttachmentInput(
                    filename="report.txt",
                    data=b"hello",
                    content_type="text/plain",
                )
            ],
        )
    msg = sent[0]
    cids = [part.get("Content-ID") for part in msg.walk() if part.get("Content-ID") is not None]
    assert any("broadcast-header" in c for c in cids)
    filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert "report.txt" in filenames
    html = msg.get_body(("html",)).get_content()
    assert "Hello team" in html
    assert 'src="cid:broadcast-header"' in html
