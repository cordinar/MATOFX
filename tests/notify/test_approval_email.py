"""Phase 5: approval notification emails.

Mirrors ``test_email.py``'s harness — monkeypatch
``matofx.notify.smtp.aiosmtplib.send`` and capture every call — so
the full flow (master-switch gate, recipient resolution, per-user
opt-out, rendering, consolidated grouping) is exercised without
binding a port.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.approvals import make_approval_service
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, EmailSettings
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.notify import ApprovalDecisionNotice, EmailNotifier, NoopNotifier
from matofx.tools.orm import Tool, ToolVersion
from matofx.users import make_password_hasher, make_user_service
from tests.notify.conftest import patch_smtp


def _text(msg: Any) -> str:
    """Decoded text/plain body of an assembled MIME message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return bytes(part.get_payload(decode=True)).decode("utf-8", "replace")
        return ""
    return bytes(msg.get_payload(decode=True)).decode("utf-8", "replace")


def _smtp() -> EmailSettings:
    return EmailSettings(
        enabled=True,
        smtp_host="smtp.invalid",
        smtp_port=2525,
        from_address="matofx@example.com",
        public_url="https://matofx.example.com",
        use_tls=False,
        use_starttls=False,
    )


async def _seed(api_engine: AsyncEngine) -> dict[str, Any]:
    """alice=requestor, bob=approver(opt-in), carol=approver(opted-out)
    + a tool + a pending pre request."""
    factory = make_session_factory(api_engine)
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    us = make_user_service(auth, audit, hasher=make_password_hasher(auth))

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        alice = await us.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="testpassword123",
            full_name="Alice Tester",
        )
        await us.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="testpassword123",
            full_name="Bob Approver",
            is_approver=True,
        )
        carol = await us.create_user(
            session,
            username="carol",
            email="carol@example.com",
            password="testpassword123",
            full_name="Carol Approver",
            is_approver=True,
        )
        carol.email_notifications_enabled = False  # opted out
        descriptor = {"alias": "demo", "approvals_policy": "pre"}
        session.add(
            Tool(
                alias="demo",
                name="Heat Transfer",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path="b",
                installed_by_user_id=alice.id,
            )
        )
        tv = ToolVersion(
            alias="demo",
            version="1.0",
            descriptor_snapshot=descriptor,
            bundle_path="b",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=alice.id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        project = Project(name="p", description="", creator_user_id=alice.id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="run-1",
            tool_alias="demo",
            tool_version_id=tv.id,
            state="pending_approval",
            inputs={},
            created_by_user_id=alice.id,
        )
        session.add(job)
        await session.flush()
        approval = make_approval_service(audit)
        req = await approval.create_request(
            session,
            job_id=job.id,
            kind="pre",
            requested_by_user_id=alice.id,
            eligible_approvers=["bob", "carol"],
            tool_alias="demo",
            tool_version_id=tv.id,
            justification="",
        )
        return {
            "factory": factory,
            "audit": audit,
            "request_id": req.id,
            "job_id": job.id,
            "alice_id": alice.id,
        }


@pytest.mark.asyncio
async def test_request_opened_emails_eligible_optin_approvers_only(
    api_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine)
    sent: list[dict[str, Any]] = []

    async def _fake(msg: Any, **_kw: Any) -> tuple[dict[str, Any], str]:
        sent.append({"to": msg["To"], "subject": msg["Subject"], "body": _text(msg)})
        return ({}, "")

    patch_smtp(monkeypatch, _fake)

    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    notifier = EmailNotifier(_smtp(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        req = await session.get(ApprovalRequest, seed["request_id"])
        await notifier.notify_approval_request_opened(session, request=req)

    # bob (opt-in) gets it; carol (opted out) does not.
    assert [s["to"] for s in sent] == ["bob@example.com"]
    msg = sent[0]
    assert "awaiting your approval" in msg["subject"].lower()
    assert "Heat Transfer" in msg["subject"]
    body = str(msg["body"])
    assert "Alice Tester" in body
    assert "/ui/approvals" in body
    assert f"{seed['job_id']:07d}" in body


@pytest.mark.asyncio
async def test_request_opened_post_kind_subject(
    api_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine)
    sent: list[dict[str, Any]] = []

    async def _fake(msg: Any, **_kw: Any) -> tuple[dict[str, Any], str]:
        sent.append({"subject": msg["Subject"]})
        return ({}, "")

    patch_smtp(monkeypatch, _fake)

    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    notifier = EmailNotifier(_smtp(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        req = await session.get(ApprovalRequest, seed["request_id"])
        req.kind = "post"
        await notifier.notify_approval_request_opened(session, request=req)

    assert "results awaiting" in sent[0]["subject"].lower()


@pytest.mark.asyncio
async def test_decisions_one_consolidated_email_per_requestor(
    api_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine)
    sent: list[dict[str, Any]] = []

    async def _fake(msg: Any, **_kw: Any) -> tuple[dict[str, Any], str]:
        sent.append({"to": msg["To"], "body": _text(msg)})
        return ({}, "")

    patch_smtp(monkeypatch, _fake)

    notices = [
        ApprovalDecisionNotice(
            job_id=11,
            job_name="A",
            tool_name="Heat Transfer",
            requestor_user_id=seed["alice_id"],
            kind="pre",
            decision="approve",
        ),
        ApprovalDecisionNotice(
            job_id=22,
            job_name="B",
            tool_name="Heat Transfer",
            requestor_user_id=seed["alice_id"],
            kind="pre",
            decision="deny",
            reason="inputs out of range",
        ),
    ]
    notifier = EmailNotifier(_smtp(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        await notifier.notify_approval_decisions(session, notices=notices)

    assert len(sent) == 1  # both notices fold into one email
    assert sent[0]["to"] == "alice@example.com"
    body = sent[0]["body"]
    assert "now queued to run" in body
    assert "inputs out of range" in body  # deny reason verbatim
    assert "0000011" in body
    assert "0000022" in body


@pytest.mark.asyncio
async def test_decisions_skips_opted_out_requestor(
    api_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine)
    # Flip alice's opt-out off → no decision email should go out.
    async with session_scope(seed["factory"]) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        alice = await session.get(User, seed["alice_id"])
        alice.email_notifications_enabled = False

    sent: list[Any] = []

    async def _fake(msg: Any, **_kw: Any) -> tuple[dict[str, Any], str]:
        sent.append(msg)
        return ({}, "")

    patch_smtp(monkeypatch, _fake)

    notifier = EmailNotifier(_smtp(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        await notifier.notify_approval_decisions(
            session,
            notices=[
                ApprovalDecisionNotice(
                    job_id=1,
                    job_name="A",
                    tool_name="T",
                    requestor_user_id=seed["alice_id"],
                    kind="pre",
                    decision="approve",
                )
            ],
        )
    assert sent == []


@pytest.mark.asyncio
async def test_noop_notifier_approval_methods_are_silent(
    api_engine: AsyncEngine,
) -> None:
    seed = await _seed(api_engine)
    notifier = NoopNotifier()
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    async with session_scope(seed["factory"]) as session:
        req = await session.get(ApprovalRequest, seed["request_id"])
        await notifier.notify_approval_request_opened(session, request=req)
        await notifier.notify_approval_decisions(session, notices=[])


# ---- generalised target descriptions + per-type / master gating ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_attr", "per_type_key", "subject_keyword"),
    [
        ("library_file_version_id", "approval_email_library", "library file"),
        ("material_version_id", "approval_email_materials", "material change"),
        ("surrogate_model_version_id", "approval_email_models", "surrogate model"),
        ("parametric_study_id", "approval_email_studies", "parametric study"),
        ("optimisation_study_id", "approval_email_optimisation", "optimisation study"),
        ("tool_vv_record_id", "approval_email_vv", "v&v record"),
    ],
)
async def test_describe_target_maps_each_kind(
    api_engine: AsyncEngine,
    target_attr: str,
    per_type_key: str,
    subject_keyword: str,
) -> None:
    """Every non-job target gets its own subject + per-type gate key."""
    import types  # noqa: PLC0415

    from matofx.notify.email import _describe_approval_target  # noqa: PLC0415

    seed = await _seed(api_engine)
    targets = (
        "job_id",
        "parametric_study_id",
        "library_file_version_id",
        "material_version_id",
        "tool_vv_record_id",
        "job_vv_record_id",
        "surrogate_model_version_id",
        "optimisation_study_id",
    )
    fields: dict[str, Any] = dict.fromkeys(targets)
    fields[target_attr] = 123
    request = types.SimpleNamespace(
        kind="pre",
        requested_by_user_id=seed["alice_id"],
        tool_alias=None,
        **fields,
    )
    async with session_scope(seed["factory"]) as session:
        info = await _describe_approval_target(session, request)
    assert info.per_type_key == per_type_key
    assert subject_keyword in info.subject.lower()


async def _set_general(seed: dict[str, Any], key: str, value: object) -> None:
    from matofx.config import make_general_settings_service  # noqa: PLC0415

    async with session_scope(seed["factory"]) as session:
        await make_general_settings_service(seed["audit"]).set(
            session,
            actor_user_id=seed["alice_id"],
            actor_role="administrator",
            key=key,
            value=value,
        )


@pytest.mark.asyncio
async def test_request_opened_silenced_by_master_switch(
    api_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine)
    await _set_general(seed, "approval_email_notifications_enabled", value=False)
    sent: list[Any] = []

    async def _fake(msg: Any, **_kw: Any) -> tuple[dict[str, Any], str]:
        sent.append(msg)
        return ({}, "")

    patch_smtp(monkeypatch, _fake)

    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    notifier = EmailNotifier(_smtp(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        req = await session.get(ApprovalRequest, seed["request_id"])
        await notifier.notify_approval_request_opened(session, request=req)
    assert sent == []


@pytest.mark.asyncio
async def test_request_opened_silenced_by_per_type_switch(
    api_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed(api_engine)
    # Master stays on; only the per-type job switch is off.
    await _set_general(seed, "approval_email_jobs", value=False)
    sent: list[Any] = []

    async def _fake(msg: Any, **_kw: Any) -> tuple[dict[str, Any], str]:
        sent.append(msg)
        return ({}, "")

    patch_smtp(monkeypatch, _fake)

    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    notifier = EmailNotifier(_smtp(), seed["audit"])
    async with session_scope(seed["factory"]) as session:
        req = await session.get(ApprovalRequest, seed["request_id"])
        await notifier.notify_approval_request_opened(session, request=req)
    assert sent == []
