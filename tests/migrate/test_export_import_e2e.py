"""End-to-end tests for the ``matofx.migrate`` export/import surface.

Each test spins up TWO SQLite DBs in a temp dir — a "source" install
and a "destination" install — and exercises the full flow:

    source.install_tool/workflow
    -> ExportService.build_bundle           (source-side reads)
    -> bundle_bytes                         (the actual artefact)
    -> ImportService.import_bundle          (destination-side writes)
    -> assertions on dest's rows + disk     (per-item outcomes)

ORM rows are inserted directly (bypassing :class:`ToolService` /
:class:`WorkflowService`) because the migrate code path is what's
under test — not the install path. This keeps the test surface tight
and avoids dragging in unrelated services. The trade-off is that the
test must mirror the same column shapes those services produce.

Coverage targets:

* Happy path: tool + workflow round-trip with descriptor / settings /
  members preserved, lifecycle state reset to ``new``.
* Trust: untrusted bundles are refused before any per-item work.
* Per-item atomicity: a missing AOS-host reference skips just the
  affected tool; cascading workflow skip cites the dep.
* Idempotency: re-importing the same bundle is a no-op.
* Conflict: same ``(alias, version)`` with a different descriptor is
  refused with a clear plain-English reason.
* Approvers: matched usernames are kept, unmatched ones dropped,
  and a tool whose approver list would be empty under a non-none
  policy is skipped.
* Dry-run: walks the same paths but commits nothing.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.config import DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.migrate import (
    ExportService,
    ImportService,
    make_export_service,
    make_import_service,
)
from matofx.migrate.errors import BundleUntrustedError
from matofx.migrate.progress import ProgressEvent
from matofx.migrate.signing import KeyPair, compute_fingerprint
from matofx.migrate.trust import TrustedPeerService, make_trusted_peer_service
from matofx.tools.exec_orm import AosHost, PythonEnv, StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.workflows.orm import Workflow, WorkflowKind, WorkflowMember
from tests._migrated_db import copy_template_to

# ---------------------------------------------------------------------------
# Engine + row-seeding helpers
# ---------------------------------------------------------------------------


def _migrate_to_head_sync(db_path: Path, template: Path) -> None:
    """Put a migrated schema at ``db_path``.

    This test needs **two** databases (a source install and a
    destination), so migrating per test cost two full 90-migration runs.
    Both are now byte copies of the session-scoped template — see
    :mod:`tests._migrated_db`.

    (The original comment about Alembic's ``env.py`` calling
    :func:`asyncio.run` still applies to the template build itself,
    which happens in the session fixture's sync setup.)
    """
    copy_template_to(template, db_path)


@pytest.fixture
def migrated_pair_paths(tmp_path: Path, migrated_template: Path) -> tuple[Path, Path]:
    """Synchronously migrate two SQLite DBs to head. Returns their paths."""
    src_path = tmp_path / "source.db"
    dst_path = tmp_path / "dest.db"
    _migrate_to_head_sync(src_path, migrated_template)
    _migrate_to_head_sync(dst_path, migrated_template)
    return src_path, dst_path


@pytest.fixture
async def install_pair(
    migrated_pair_paths: tuple[Path, Path],
) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    """Yield two freshly-migrated SQLite engines: source, destination."""
    src_path, dst_path = migrated_pair_paths
    src_settings = DbSettings.from_url(f"sqlite:///{src_path}", dialect="sqlite")
    dst_settings = DbSettings.from_url(f"sqlite:///{dst_path}", dialect="sqlite")
    src_engine = make_engine(src_settings)
    dst_engine = make_engine(dst_settings)
    try:
        yield src_engine, dst_engine
    finally:
        await src_engine.dispose()
        await dst_engine.dispose()


def _make_descriptor(
    *,
    alias: str = "thermal",
    version: str = "1.5.0",
    name: str = "Thermal Tool",
    approvals_policy: str = "none",
    approver_usernames: list[str] | None = None,
    licences_required: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a valid descriptor JSON dict for use in seeded Tool rows."""
    descriptor: dict[str, Any] = {
        "alias": alias,
        "name": name,
        "version": version,
        "description": f"Test fixture for {alias}",
        "main_script": "static/main.py",
        "approvals_policy": approvals_policy,
        "approver_usernames": approver_usernames or [],
    }
    if licences_required is not None:
        descriptor["licences_required"] = licences_required
    return descriptor


async def _seed_admin(engine: AsyncEngine, username: str = "ada") -> int:
    """Seed one admin user, looking up the role seeded by migration 0006."""
    from matofx.users.orm import User
    from matofx.users.role_orm import Role

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        admin_role = (
            await session.execute(select(Role).where(Role.name == "administrator"))
        ).scalar_one()
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="$argon2id$placeholder",
            full_name=username.title(),
            role_id=admin_role.id,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        return user.id


async def _seed_aos_host(engine: AsyncEngine, name: str) -> int:
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        row = AosHost(
            name=name,
            hostname=f"{name}.internal",
            os="linux",
            transport="ssh",
            port=22,
            username="matofx",
            secret_ref=f"vault://{name}",
            is_active=True,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _seed_python_env(
    engine: AsyncEngine, name: str, storage_location_id: int | None = None
) -> int:
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        row = PythonEnv(
            name=name,
            interpreter_path=f"/opt/{name}/bin/python",
            storage_location_id=storage_location_id,
            is_active=True,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _seed_storage_location(engine: AsyncEngine, name: str) -> int:
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        row = StorageLocation(
            name=name,
            mount_path=f"/mnt/{name}",
            is_active=True,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _seed_user(engine: AsyncEngine, username: str, *, role: str = "regular_user") -> int:
    """Seed one extra user beyond the admin. Returns the user's id."""
    from matofx.users.orm import User
    from matofx.users.role_orm import Role

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        role_row = (await session.execute(select(Role).where(Role.name == role))).scalar_one()
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="$argon2id$placeholder",
            full_name=username.title(),
            role_id=role_row.id,
            is_active=True,
            is_approver=True,
        )
        session.add(user)
        await session.flush()
        return user.id


async def _seed_tool(
    engine: AsyncEngine,
    *,
    installed_by: int,
    bundle_root: Path,
    descriptor: dict[str, Any] | None = None,
    aos_host_id: int | None = None,
    python_env_id: int | None = None,
    execution_target: str = "local",
    state: str = "released",
    bundle_files: dict[str, bytes] | None = None,
) -> Tool:
    """Insert a Tool + ToolVersion row + write static bundle files to disk."""
    descriptor = descriptor or _make_descriptor()
    alias = descriptor["alias"]
    version = descriptor["version"]
    bundle_path = bundle_root / alias / version
    bundle_path.mkdir(parents=True, exist_ok=True)
    files = bundle_files or {
        "descriptor.json": b"{}",
        "static/main.py": b'print("hello")\n',
    }
    for rel, data in files.items():
        dest = bundle_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        tool = Tool(
            alias=alias,
            name=descriptor.get("name", alias),
            version=version,
            state=state,
            descriptor=descriptor,
            bundle_path=str(bundle_path),
            installed_by_user_id=installed_by,
            execution_target=execution_target,
            aos_host_id=aos_host_id,
            python_env_id=python_env_id,
        )
        session.add(tool)
        await session.flush()
        version_row = ToolVersion(
            alias=alias,
            version=version,
            descriptor_snapshot=descriptor,
            bundle_path=str(bundle_path),
            created_by_user_id=installed_by,
            change_summary="Initial install (test fixture).",
            changes={},
        )
        session.add(version_row)
        await session.flush()
        return tool


async def _seed_admin_workflow(
    engine: AsyncEngine,
    *,
    installed_by: int,
    alias: str,
    name: str,
    member_aliases: list[str],
    state: str = "released",
) -> Workflow:
    """Insert an admin-released Workflow + WorkflowMember rows."""
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        workflow = Workflow(
            alias=alias,
            name=name,
            version="1.0.0",
            kind=WorkflowKind.ADMIN.value,
            state=state,
            descriptor={"description": f"Test workflow {alias}", "teams": []},
            installed_by_user_id=installed_by,
        )
        session.add(workflow)
        await session.flush()
        for position, tool_alias in enumerate(member_aliases, start=1):
            session.add(
                WorkflowMember(
                    workflow_id=workflow.id,
                    tool_alias=tool_alias,
                    position=position,
                )
            )
        await session.flush()
        return workflow


# ---------------------------------------------------------------------------
# Shared service-construction helpers
# ---------------------------------------------------------------------------


def _make_services() -> tuple[AuditWriter, ExportService]:
    writer = AuditWriter(signing_key=None, key_id=None)
    return writer, make_export_service(writer)


def _make_dest_services(bundle_root: Path) -> tuple[AuditWriter, TrustedPeerService, ImportService]:
    writer = AuditWriter(signing_key=None, key_id=None)
    trust = make_trusted_peer_service(writer)
    import_svc = make_import_service(writer, trust_service=trust, bundle_root=bundle_root)
    return writer, trust, import_svc


async def _trust(
    engine: AsyncEngine,
    trust_svc: TrustedPeerService,
    *,
    actor_user_id: int,
    fingerprint: str,
    label: str = "Source",
) -> None:
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        await trust_svc.add_peer(
            session,
            actor_user_id=actor_user_id,
            fingerprint=fingerprint,
            label=label,
        )


def _tar_members(bundle_bytes: bytes) -> dict[str, bytes]:
    """Return every regular-file member of the gzipped-tarball bundle."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        for info in tar.getmembers():
            if not info.isreg():
                continue
            extracted = tar.extractfile(info)
            if extracted is not None:
                out[info.name] = extracted.read()
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_simple_tool(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_bundle_root = tmp_path / "src_bundles"
    dst_bundle_root = tmp_path / "dst_bundles"

    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)

    descriptor = _make_descriptor(alias="thermal", version="1.5.0")
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=src_bundle_root,
        descriptor=descriptor,
    )

    _src_audit, export_svc = _make_services()
    _dst_audit, trust_svc, import_svc = _make_dest_services(dst_bundle_root)

    kp = KeyPair.generate()
    fingerprint = compute_fingerprint(kp.public_key)
    await _trust(dst_engine, trust_svc, actor_user_id=dst_admin, fingerprint=fingerprint)

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="source-uuid",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
            notes="happy path",
        )

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session,
            actor_user_id=dst_admin,
            bundle_bytes=bundle_bytes,
        )

    assert result.imported_count == 1
    assert result.skipped_count == 0

    async with session_scope(dst_factory) as session:
        tool = (await session.execute(select(Tool).where(Tool.alias == "thermal"))).scalar_one()
        assert tool.version == "1.5.0"
        # Per feedback_migrate_reset_to_new — state always resets.
        assert tool.state == "new"
        assert tool.descriptor["alias"] == "thermal"
        version_rows = (
            (await session.execute(select(ToolVersion).where(ToolVersion.alias == "thermal")))
            .scalars()
            .all()
        )
        assert len(version_rows) == 1
        assert version_rows[0].version == "1.5.0"

    # Bundle files landed under the dest's bundle root.
    target_dir = dst_bundle_root / "thermal" / "1.5.0"
    assert (target_dir / "descriptor.json").exists()
    assert (target_dir / "static" / "main.py").read_bytes() == b'print("hello")\n'


@pytest.mark.asyncio
async def test_round_trip_workflow_with_member_tools(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_bundle_root = tmp_path / "src_bundles"
    dst_bundle_root = tmp_path / "dst_bundles"

    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)

    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=src_bundle_root,
        descriptor=_make_descriptor(alias="alpha", version="1.0.0"),
    )
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=src_bundle_root,
        descriptor=_make_descriptor(alias="beta", version="1.0.0"),
    )
    await _seed_admin_workflow(
        src_engine,
        installed_by=src_admin,
        alias="alpha_then_beta",
        name="Alpha then Beta",
        member_aliases=["alpha", "beta"],
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(dst_bundle_root)
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("alpha", "1.0.0"), ("beta", "1.0.0")],
            workflow_aliases=["alpha_then_beta"],
        )

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
        )

    # 2 tools + 1 workflow imported.
    assert result.imported_count == 3
    async with session_scope(dst_factory) as session:
        workflow = (
            await session.execute(select(Workflow).where(Workflow.alias == "alpha_then_beta"))
        ).scalar_one()
        assert workflow.state == "new"
        members = (
            (
                await session.execute(
                    select(WorkflowMember).where(WorkflowMember.workflow_id == workflow.id)
                )
            )
            .scalars()
            .all()
        )
        assert sorted((m.tool_alias, m.position) for m in members) == [
            ("alpha", 1),
            ("beta", 2),
        ]


# ---------------------------------------------------------------------------
# Trust + signature failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_untrusted_bundle_refused(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)

    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor=_make_descriptor(),
    )

    _, export_svc = _make_services()
    _, _trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
        )

    # Note: trust list NOT seeded on dest.
    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(BundleUntrustedError) as exc_info:
            await import_svc.import_bundle(
                session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
            )
        assert "trusted peer list" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Per-item skip: missing reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_aos_host_skips_tool_and_cascades_workflow(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_bundle_root = tmp_path / "src"

    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)
    src_aos = await _seed_aos_host(src_engine, "ansys_main")

    # Source tool binds to an AOS host that doesn't exist on dest.
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=src_bundle_root,
        descriptor=_make_descriptor(alias="heat_transfer", version="2.1"),
        aos_host_id=src_aos,
        execution_target="aos",
    )
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=src_bundle_root,
        descriptor=_make_descriptor(alias="material_props", version="1.5.0"),
    )
    await _seed_admin_workflow(
        src_engine,
        installed_by=src_admin,
        alias="thermal_pipeline",
        name="Thermal Pipeline",
        member_aliases=["material_props", "heat_transfer"],
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("heat_transfer", "2.1"), ("material_props", "1.5.0")],
            workflow_aliases=["thermal_pipeline"],
        )

    events: list[ProgressEvent] = []
    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session,
            actor_user_id=dst_admin,
            bundle_bytes=bundle_bytes,
            on_event=events.append,
        )

    # material_props imports; heat_transfer skipped; workflow cascades.
    by_alias = {(o.kind, o.alias): o for o in result.items}
    assert by_alias[("tool", "material_props")].outcome == "imported"
    assert by_alias[("tool", "heat_transfer")].outcome == "skipped"
    assert by_alias[("tool", "heat_transfer")].reason is not None
    assert "AOS host" in by_alias[("tool", "heat_transfer")].reason
    assert "ansys_main" in by_alias[("tool", "heat_transfer")].reason
    assert by_alias[("workflow", "thermal_pipeline")].outcome == "skipped"
    assert "heat_transfer" in by_alias[("workflow", "thermal_pipeline")].reason

    # Skip events emitted with proper item attribution.
    skip_events = [e for e in events if e.level == "skipped"]
    assert any(e.item_alias == "heat_transfer" for e in skip_events)
    assert any(e.item_alias == "thermal_pipeline" for e in skip_events)


# ---------------------------------------------------------------------------
# Idempotency + conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reimport_same_bundle_is_noop(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor=_make_descriptor(alias="thermal", version="1.5.0"),
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
        )

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        first = await import_svc.import_bundle(
            session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
        )
        assert first.imported_count == 1

    async with session_scope(dst_factory) as session:
        second = await import_svc.import_bundle(
            session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
        )
    # Same descriptor hash on a second pass → no-op, not skip, not import.
    assert second.imported_count == 0
    assert second.skipped_count == 0
    assert second.noop_count == 1


@pytest.mark.asyncio
async def test_version_hash_conflict_is_skipped(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)

    # Source ships thermal 1.5 with description X.
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor={
            **_make_descriptor(alias="thermal", version="1.5.0"),
            "description": "Version made on source",
        },
    )
    # Dest already has thermal 1.5 with a DIFFERENT description.
    await _seed_tool(
        dst_engine,
        installed_by=dst_admin,
        bundle_root=tmp_path / "dst_existing",
        descriptor={
            **_make_descriptor(alias="thermal", version="1.5.0"),
            "description": "Version already on dest",
        },
        state="released",
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
        )

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
        )

    assert result.skipped_count == 1
    item = result.items[0]
    assert item.outcome == "skipped"
    assert "different descriptor" in item.reason


# ---------------------------------------------------------------------------
# Approvers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approver_filter_drops_unmatched_keeps_matched(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine, username="src_admin")
    dst_admin = await _seed_admin(dst_engine, username="dst_admin")

    # Two users on source as approvers; only one of them exists on dest.
    await _seed_user(src_engine, "alice", role="regular_user")
    await _seed_user(src_engine, "bob", role="regular_user")
    await _seed_user(dst_engine, "alice", role="regular_user")

    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor=_make_descriptor(
            alias="thermal",
            version="1.5.0",
            approvals_policy="pre",
            approver_usernames=["alice", "bob"],
        ),
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
        )

    events: list[ProgressEvent] = []
    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session,
            actor_user_id=dst_admin,
            bundle_bytes=bundle_bytes,
            on_event=events.append,
        )

    assert result.imported_count == 1
    async with session_scope(dst_factory) as session:
        tool = (await session.execute(select(Tool).where(Tool.alias == "thermal"))).scalar_one()
        assert tool.descriptor["approver_usernames"] == ["alice"]

    # A warning event surfaced the dropped approver.
    assert any(e.level == "warning" and "bob" in e.message for e in events)


@pytest.mark.asyncio
async def test_approver_filter_all_missing_skips_when_policy_required(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine, username="src_admin")
    dst_admin = await _seed_admin(dst_engine, username="dst_admin")

    await _seed_user(src_engine, "alice", role="regular_user")
    # Dest has NO matching user.

    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor=_make_descriptor(
            alias="thermal",
            version="1.5.0",
            approvals_policy="pre",
            approver_usernames=["alice"],
        ),
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
        )

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
        )

    assert result.skipped_count == 1
    reason = result.items[0].reason
    assert "approvals policy" in reason.lower()
    assert "alice" in reason


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor=_make_descriptor(alias="thermal", version="1.5.0"),
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
        )

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session,
            actor_user_id=dst_admin,
            bundle_bytes=bundle_bytes,
            dry_run=True,
        )

    # Dry-run reports the would-be outcome ...
    assert result.dry_run is True
    assert result.imported_count == 1
    # ... but no Tool row was inserted, no bundle dir was created, and
    # no migrate.bundle.imported event was audited.
    async with session_scope(dst_factory) as session:
        tools = (await session.execute(select(Tool))).scalars().all()
        assert tools == []
        audit_events = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "migrate.bundle.imported")
                )
            )
            .scalars()
            .all()
        )
        assert audit_events == []
    assert not (tmp_path / "dst_bundles" / "thermal" / "1.5.0").exists()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_and_import_each_write_one_audit_event(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    src_engine, dst_engine = install_pair
    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=tmp_path / "src",
        descriptor=_make_descriptor(),
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(tmp_path / "dst_bundles")
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src-uuid",
            tool_specs=[("thermal", "1.5.0")],
            workflow_aliases=[],
            notes="ticket MTX-412",
        )

    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()

    async with session_scope(src_factory) as session:
        export_events = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "migrate.bundle.exported")
                )
            )
            .scalars()
            .all()
        )
        assert len(export_events) == 1
        assert export_events[0].payload["bundle_hash"] == bundle_hash
        assert export_events[0].payload["notes"] == "ticket MTX-412"
        assert export_events[0].payload["tools"] == ["thermal:1.5.0"]

    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        await import_svc.import_bundle(session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes)

    async with session_scope(dst_factory) as session:
        import_events = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "migrate.bundle.imported")
                )
            )
            .scalars()
            .all()
        )
        assert len(import_events) == 1
        payload = import_events[0].payload
        assert payload["bundle_hash"] == bundle_hash
        assert payload["source_fingerprint"] == compute_fingerprint(kp.public_key)
        assert payload["source_label"] == "Source"
        assert len(payload["outcomes"]) == 1
        assert payload["outcomes"][0]["outcome"] == "imported"


# ---------------------------------------------------------------------------
# Authoritative descriptor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_ships_authoritative_descriptor_not_stale_on_disk_file(
    install_pair: tuple[AsyncEngine, AsyncEngine], tmp_path: Path
) -> None:
    """The bundle carries the DB descriptor as descriptor.json, and drops the
    stale on-disk descriptor.toml (which lags the DB after version bumps)."""
    src_engine, dst_engine = install_pair
    src_bundle_root = tmp_path / "src"
    dst_bundle_root = tmp_path / "dst_bundles"

    src_admin = await _seed_admin(src_engine)
    dst_admin = await _seed_admin(dst_engine)

    # DB descriptor is authoritative at 1.0.7; the on-disk authoring file is a
    # stale descriptor.toml frozen at 1.0.0 (the drift this fix addresses).
    descriptor = _make_descriptor(alias="bracket", version="1.0.7")
    await _seed_tool(
        src_engine,
        installed_by=src_admin,
        bundle_root=src_bundle_root,
        descriptor=descriptor,
        bundle_files={
            "descriptor.toml": b'alias = "bracket"\nversion = "1.0.0"\n',
            "static/main.py": b'print("hi")\n',
        },
    )

    _, export_svc = _make_services()
    _, trust_svc, import_svc = _make_dest_services(dst_bundle_root)
    kp = KeyPair.generate()
    await _trust(
        dst_engine,
        trust_svc,
        actor_user_id=dst_admin,
        fingerprint=compute_fingerprint(kp.public_key),
    )

    src_factory = make_session_factory(src_engine)
    async with session_scope(src_factory) as session:
        await ensure_chain_head(session)
        bundle_bytes = await export_svc.build_bundle(
            session,
            actor_user_id=src_admin,
            key_pair=kp,
            source_install_id="src",
            tool_specs=[("bracket", "1.0.7")],
            workflow_aliases=[],
        )

    # The bundle ships descriptor.json (version 1.0.7), not the stale .toml.
    members = _tar_members(bundle_bytes)
    assert "tools/bracket-1.0.7/descriptor.json" in members
    assert "tools/bracket-1.0.7/descriptor.toml" not in members
    shipped = json.loads(members["tools/bracket-1.0.7/descriptor.json"])
    assert shipped["version"] == "1.0.7"
    # Static files are untouched.
    assert members["tools/bracket-1.0.7/static/main.py"] == b'print("hi")\n'

    # And it round-trips: the destination lands descriptor.json at 1.0.7 and
    # no stale descriptor.toml.
    dst_factory = make_session_factory(dst_engine)
    async with session_scope(dst_factory) as session:
        await ensure_chain_head(session)
        result = await import_svc.import_bundle(
            session, actor_user_id=dst_admin, bundle_bytes=bundle_bytes
        )
    assert result.imported_count == 1
    target_dir = dst_bundle_root / "bracket" / "1.0.7"
    on_disk = json.loads((target_dir / "descriptor.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == "1.0.7"
    assert not (target_dir / "descriptor.toml").exists()
