"""Service tests for the file-library lifecycle (slice 2).

Covers the design §11 plan that lives at the service layer: immutable
versioning, scope visibility, tool resolution, the directed-approver
publish gate (toggle on/off, no self-approval, approve/reject), retire
is state-only, optimistic locking, and access-checked byte reads.
``conftest.basics`` seeds the FK parents; a per-test
:class:`StorageLocation` is added so ``create_file`` / ``add_version``
can stage bytes under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.approvals import ApprovalRequest
from matofx.approvals.errors import SelfApprovalNotAllowedError
from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.library import (
    ApproverRequiredError,
    DuplicateToolAssignmentError,
    GlobalPublishNotPermittedError,
    InvalidScopeError,
    LibraryConcurrentModificationError,
    LibraryFile,
    LibraryFileNotFoundError,
    LibraryFileRetiredError,
    LibraryFileVersion,
    LibraryService,
    LibraryValidationError,
    ProjectScopeError,
    ToolAssignmentRequiredError,
    ToolAssignmentSpec,
    make_library_service,
)
from matofx.library.orm import JobLibraryFileUse
from matofx.tools.exec_orm import StorageLocation

from .conftest import Basics


@dataclass
class Env:
    """A wired-up service plus the ids / paths a test needs."""

    svc: LibraryService
    factory: object
    basics: Basics
    mount: Path

    @property
    def sam(self) -> int:
        """The super_user (also a library curator)."""
        return self.basics.user_id

    @property
    def ravi(self) -> int:
        """A regular user — no curator capability."""
        return self.basics.other_user_id


@pytest.fixture
async def env(library_engine: AsyncEngine, basics: Basics, tmp_path: Path) -> Env:
    """Service + an active StorageLocation rooted at ``tmp_path``."""
    mount = tmp_path / "mount"
    mount.mkdir()
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        session.add(StorageLocation(name="primary", mount_path=str(mount)))
    svc = make_library_service(AuditWriter(signing_key=None, key_id=None), environment="test")
    return Env(svc=svc, factory=factory, basics=basics, mount=mount)


def _geom() -> tuple[str, bytes]:
    return "baseline.step", b"ISO-10303-21; STEP geometry payload"


async def _create_personal(env: Env, owner: int, **overrides: object) -> int:
    """Create a personal file owned by ``owner``; return its id."""
    name, content = _geom()
    kwargs: dict[str, object] = {
        "name": "Baseline geometry",
        "description": "shared STEP",
        "original_filename": name,
        "content": content,
        "provenance": "exported from CAD",
        "tool_assignments": [ToolAssignmentSpec("demo", "geometry")],
    }
    kwargs.update(overrides)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        lib = await env.svc.create_file(session, actor_user_id=owner, **kwargs)  # type: ignore[arg-type]
        return lib.id


# --------------------------------------------------------------------------
# create + immutable versioning
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_personal_publishes_first_version_and_stores_bytes(env: Env) -> None:
    name, content = _geom()
    file_id = await _create_personal(env, env.sam)

    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        versions = await env.svc.list_versions(session, actor_user_id=env.sam, file_id=file_id)
        assert len(versions) == 1
        v = versions[0]
        assert v.ordinal == 1
        assert v.publish_state == "published"  # personal publishes immediately
        assert v.original_filename == name
        assert v.content_hash == hashlib.sha256(content).hexdigest()
        assert v.extension == "step"
        # bytes really landed on the mount
        assert Path(v.stored_path).read_bytes() == content
        assert str(env.mount) in v.stored_path

        tools = await env.svc.list_tool_assignments(session, actor_user_id=env.sam, file_id=file_id)
        assert [(t.tool_alias, t.field_name) for t in tools] == [("demo", "geometry")]


@pytest.mark.asyncio
async def test_add_version_is_append_only_and_leaves_old_bytes_untouched(env: Env) -> None:
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        v1 = (await env.svc.list_versions(session, actor_user_id=env.sam, file_id=file_id))[0]
        v1_hash, v1_path = v1.content_hash, v1.stored_path

    new_bytes = b"ISO-10303-21; revised geometry"
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        v2 = await env.svc.add_version(
            session,
            actor_user_id=env.sam,
            file_id=file_id,
            original_filename="rev2.step",
            content=new_bytes,
            provenance="re-exported",
        )
        assert v2.ordinal == 2
        assert v2.publish_state == "published"

    # the original version's bytes + hash are unchanged
    assert Path(v1_path).read_bytes() == _geom()[1]
    assert v1_hash == hashlib.sha256(_geom()[1]).hexdigest()
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        versions = await env.svc.list_versions(session, actor_user_id=env.sam, file_id=file_id)
        assert [v.ordinal for v in versions] == [2, 1]


@pytest.mark.asyncio
async def test_create_requires_name_provenance_and_tool(env: Env) -> None:
    name, content = _geom()
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(LibraryValidationError):
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                name="   ",
                description="",
                original_filename=name,
                content=content,
                provenance="cad",
                tool_assignments=[ToolAssignmentSpec("demo")],
            )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(LibraryValidationError):
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                name="x",
                description="",
                original_filename=name,
                content=content,
                provenance="  ",
                tool_assignments=[ToolAssignmentSpec("demo")],
            )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(ToolAssignmentRequiredError):
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                name="x",
                description="",
                original_filename=name,
                content=content,
                provenance="cad",
                tool_assignments=[],
            )


# --------------------------------------------------------------------------
# scope coherence + visibility
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_project_coherence_checks(env: Env) -> None:
    name, content = _geom()
    common: dict[str, object] = {
        "name": "x",
        "description": "",
        "original_filename": name,
        "content": content,
        "provenance": "cad",
        "tool_assignments": [ToolAssignmentSpec("demo")],
    }
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(InvalidScopeError):
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                scope="team",
                **common,  # type: ignore[arg-type]
            )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(ProjectScopeError):  # project scope, no project_id
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                scope="project",
                **common,  # type: ignore[arg-type]
            )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(ProjectScopeError):  # personal scope with a project_id
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                scope="personal",
                project_id=env.basics.project_id,
                **common,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_personal_file_invisible_to_non_owner(env: Env) -> None:
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # owner sees it
        assert (await env.svc.get_file(session, actor_user_id=env.sam, file_id=file_id)).id == (
            file_id
        )
        # a regular non-owner does not (existence-hidden)
        with pytest.raises(LibraryFileNotFoundError):
            await env.svc.get_file(session, actor_user_id=env.ravi, file_id=file_id)
        # ...and can't version it either (visibility gate runs first)
        with pytest.raises(LibraryFileNotFoundError):
            await env.svc.add_version(
                session,
                actor_user_id=env.ravi,
                file_id=file_id,
                original_filename="x.step",
                content=b"x",
                provenance="y",
            )


@pytest.mark.asyncio
async def test_project_file_visible_to_project_members_only(env: Env) -> None:
    # sam is the project's creator (OWNER) → may create a project-scoped file.
    name, content = _geom()
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        lib = await env.svc.create_file(
            session,
            actor_user_id=env.sam,
            name="bc",
            description="",
            original_filename=name,
            content=content,
            provenance="cad",
            tool_assignments=[ToolAssignmentSpec("demo")],
            scope="project",
            project_id=env.basics.project_id,
        )
        file_id = lib.id
        assert lib.scope == "project"
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # ravi has no ACL on the project → can't see it.
        with pytest.raises(LibraryFileNotFoundError):
            await env.svc.get_file(session, actor_user_id=env.ravi, file_id=file_id)


# --------------------------------------------------------------------------
# tool assignment layer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_unassign_and_last_assignment_protected(env: Env) -> None:
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # duplicate of the create-time assignment is rejected
        with pytest.raises(DuplicateToolAssignmentError):
            await env.svc.assign_tool(
                session,
                actor_user_id=env.sam,
                file_id=file_id,
                tool_alias="demo",
                field_name="geometry",
            )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        await env.svc.assign_tool(
            session, actor_user_id=env.sam, file_id=file_id, tool_alias="demo", field_name="mesh"
        )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        tools = await env.svc.list_tool_assignments(session, actor_user_id=env.sam, file_id=file_id)
        assert len(tools) == 2
        # removing one is fine
        await env.svc.unassign_tool(
            session, actor_user_id=env.sam, file_id=file_id, tool_alias="demo", field_name="mesh"
        )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # removing the final assignment is blocked
        with pytest.raises(ToolAssignmentRequiredError):
            await env.svc.unassign_tool(
                session,
                actor_user_id=env.sam,
                file_id=file_id,
                tool_alias="demo",
                field_name="geometry",
            )


# --------------------------------------------------------------------------
# metadata + optimistic lock
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_metadata_optimistic_lock(env: Env) -> None:
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        lib = await env.svc.update_metadata(
            session, actor_user_id=env.sam, file_id=file_id, expected_version=1, name="renamed"
        )
        assert lib.name == "renamed"
        assert lib.version == 2
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(LibraryConcurrentModificationError):
            await env.svc.update_metadata(
                session, actor_user_id=env.sam, file_id=file_id, expected_version=1, name="again"
            )


# --------------------------------------------------------------------------
# retire / restore (state-only, no hard delete)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_is_state_only_and_blocks_mutation(env: Env) -> None:
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        await env.svc.retire(session, actor_user_id=env.sam, file_id=file_id)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        row = await session.get(LibraryFile, file_id)
        assert row is not None  # row persists (no hard delete)
        assert row.state == "retired"
        with pytest.raises(LibraryFileRetiredError):
            await env.svc.update_metadata(
                session, actor_user_id=env.sam, file_id=file_id, name="nope"
            )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        restored = await env.svc.restore(session, actor_user_id=env.sam, file_id=file_id)
        assert restored.state == "active"


# --------------------------------------------------------------------------
# global publish gate (ADR-0006 §5)
# --------------------------------------------------------------------------


async def _create_global_pending(env: Env) -> tuple[int, int, int]:
    """ravi proposes a global file with sam as approver. Returns ids."""
    name, content = _geom()
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        lib = await env.svc.create_file(
            session,
            actor_user_id=env.ravi,
            name="global geom",
            description="",
            original_filename=name,
            content=content,
            provenance="cad",
            tool_assignments=[ToolAssignmentSpec("demo", "geometry")],
            scope="global",
            approval_required=True,
            designated_approver_user_id=env.sam,
            designated_approver_username="sam",
        )
        file_id = lib.id
        assert lib.state == "pending"
        version = (
            await session.execute(
                select(LibraryFileVersion).where(LibraryFileVersion.library_file_id == file_id)
            )
        ).scalar_one()
        assert version.publish_state == "pending"
        request = (
            await session.execute(
                select(ApprovalRequest).where(ApprovalRequest.library_file_version_id == version.id)
            )
        ).scalar_one()
        assert request.designated_approver_user_id == env.sam
        return file_id, version.id, request.id


@pytest.mark.asyncio
async def test_global_create_requires_approver_when_gate_on(env: Env) -> None:
    name, content = _geom()
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(ApproverRequiredError):
            await env.svc.create_file(
                session,
                actor_user_id=env.ravi,
                name="g",
                description="",
                original_filename=name,
                content=content,
                provenance="cad",
                tool_assignments=[ToolAssignmentSpec("demo")],
                scope="global",
                approval_required=True,
            )


@pytest.mark.asyncio
async def test_global_create_rejects_self_approval(env: Env) -> None:
    name, content = _geom()
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(SelfApprovalNotAllowedError):
            await env.svc.create_file(
                session,
                actor_user_id=env.sam,
                name="g",
                description="",
                original_filename=name,
                content=content,
                provenance="cad",
                tool_assignments=[ToolAssignmentSpec("demo")],
                scope="global",
                approval_required=True,
                designated_approver_user_id=env.sam,
                designated_approver_username="sam",
            )


@pytest.mark.asyncio
async def test_pending_global_invisible_in_picker_until_approved(env: Env) -> None:
    file_id, _version_id, request_id = await _create_global_pending(env)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        picks = await env.svc.list_for_picker(
            session, actor_user_id=env.ravi, tool_alias="demo", field_name="geometry"
        )
        assert picks == []  # pending → not pickable
    # sam (curator + designated approver) approves
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        request, version = await env.svc.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=env.sam,
            decider_username="sam",
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "approved"
        assert version.publish_state == "published"
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        row = await session.get(LibraryFile, file_id)
        assert row is not None
        assert row.state == "active"
        # now a regular user sees it in the picker (global, active, published)
        picks = await env.svc.list_for_picker(
            session, actor_user_id=env.ravi, tool_alias="demo", field_name="geometry"
        )
        assert [p.file_id for p in picks] == [file_id]


@pytest.mark.asyncio
async def test_global_reject_marks_version_rejected(env: Env) -> None:
    file_id, version_id, request_id = await _create_global_pending(env)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        request, version = await env.svc.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=env.sam,
            decider_username="sam",
            decision="deny",
            reason="needs a cleaner mesh",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "denied"
        assert version.publish_state == "rejected"
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # not pickable; the file row persists (no hard delete)
        assert await session.get(LibraryFile, file_id) is not None
        assert await session.get(LibraryFileVersion, version_id) is not None


@pytest.mark.asyncio
async def test_gate_off_curator_publishes_directly_noncurator_blocked(env: Env) -> None:
    name, content = _geom()
    # sam is a curator → direct publish to global with the gate off
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        lib = await env.svc.create_file(
            session,
            actor_user_id=env.sam,
            name="curated",
            description="",
            original_filename=name,
            content=content,
            provenance="cad",
            tool_assignments=[ToolAssignmentSpec("demo", "geometry")],
            scope="global",
            approval_required=False,
        )
        assert lib.state == "active"
        version = (
            await session.execute(
                select(LibraryFileVersion).where(LibraryFileVersion.library_file_id == lib.id)
            )
        ).scalar_one()
        assert version.publish_state == "published"
    # ravi is not a curator → blocked from direct global publish
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(GlobalPublishNotPermittedError):
            await env.svc.create_file(
                session,
                actor_user_id=env.ravi,
                name="sneaky",
                description="",
                original_filename=name,
                content=content,
                provenance="cad",
                tool_assignments=[ToolAssignmentSpec("demo")],
                scope="global",
                approval_required=False,
            )


# --------------------------------------------------------------------------
# picker tool resolution (field-pin vs null-field extension filter)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_picker_field_pin_and_null_field_extension_filter(env: Env) -> None:
    # A: pinned to demo/geometry, .step
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        a = await env.svc.create_file(
            session,
            actor_user_id=env.sam,
            name="A",
            description="",
            original_filename="a.step",
            content=b"step",
            provenance="cad",
            tool_assignments=[ToolAssignmentSpec("demo", "geometry")],
        )
        a_id = a.id
    # B: null-field on demo (any compatible field), .csv
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        b = await env.svc.create_file(
            session,
            actor_user_id=env.sam,
            name="B",
            description="",
            original_filename="b.csv",
            content=b"a,b,c",
            provenance="export",
            tool_assignments=[ToolAssignmentSpec("demo", None)],
        )
        b_id = b.id

    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # geometry field accepting .step → A (pinned). B is null-field but csv∉{step}.
        geo = await env.svc.list_for_picker(
            session,
            actor_user_id=env.sam,
            tool_alias="demo",
            field_name="geometry",
            accepted_extensions={"step"},
        )
        assert [p.file_id for p in geo] == [a_id]
        # temps field accepting .csv → B (null-field, csv∈{csv}). A isn't assigned to temps.
        temps = await env.svc.list_for_picker(
            session,
            actor_user_id=env.sam,
            tool_alias="demo",
            field_name="temps",
            accepted_extensions={"csv"},
        )
        assert [p.file_id for p in temps] == [b_id]


# --------------------------------------------------------------------------
# byte reads + provenance
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_version_bytes_access_checked(env: Env) -> None:
    _name, content = _geom()
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        version = (await env.svc.list_versions(session, actor_user_id=env.sam, file_id=file_id))[0]
        v_id = version.id
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        got_version, got_bytes = await env.svc.open_version_bytes(
            session, actor_user_id=env.sam, version_id=v_id
        )
        assert got_bytes == content
        assert got_version.id == v_id
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        # ravi can't read sam's personal version
        with pytest.raises(LibraryFileNotFoundError):
            await env.svc.open_version_bytes(session, actor_user_id=env.ravi, version_id=v_id)


@pytest.mark.asyncio
async def test_record_job_use_writes_provenance(env: Env) -> None:
    file_id = await _create_personal(env, env.sam)
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        version = (await env.svc.list_versions(session, actor_user_id=env.sam, file_id=file_id))[0]
        v_id = version.id
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        await env.svc.record_job_use(
            session,
            job_id=env.basics.job_id,
            field_name="geometry",
            library_file_version_id=v_id,
        )
    async with session_scope(env.factory) as session:  # type: ignore[arg-type]
        row = await session.get(JobLibraryFileUse, (env.basics.job_id, "geometry"))
        assert row is not None
        assert row.library_file_version_id == v_id
