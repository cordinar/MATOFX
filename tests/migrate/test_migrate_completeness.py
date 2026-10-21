"""ADR-0023 — migrate completeness: links, metadata and dependency rules.

Two SQLite installs per test (source + destination), exactly like
``test_export_import_e2e.py``, whose seeding helpers and fixtures this
module reuses. Each test pins one rule from ``designs/migrate-completeness.md``.
"""

from __future__ import annotations

import base64
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.migrate import ExportService, ImportService, make_export_service, make_import_service
from matofx.migrate import bundle as bundle_mod
from matofx.migrate.bundle import BundleError, ToolManifestEntry, ToolPayload, pack_bundle
from matofx.migrate.errors import SurrogateToolNotExportableError
from matofx.migrate.progress import ProgressEvent
from matofx.migrate.service import ImportResult
from matofx.migrate.signing import KeyPair, compute_fingerprint, sign_manifest
from matofx.migrate.trust import make_trusted_peer_service
from matofx.tools.descriptor import ToolDescriptor
from matofx.tools.errors import TeamsPendingError
from matofx.tools.orm import Tool, ToolVersion
from matofx.tools.service import make_tool_service
from matofx.users.orm import User
from matofx.users.team_orm import Team
from matofx.workflows.errors import WorkflowStateError
from matofx.workflows.orm import Workflow, WorkflowKind, WorkflowMember, WorkflowVersion
from matofx.workflows.service import make_workflow_service
from tests.migrate.test_export_import_e2e import (  # noqa: F401 — fixtures re-exported
    _seed_admin,
    _seed_admin_workflow,
    _seed_tool,
    _seed_user,
    install_pair,
    migrated_pair_paths,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _desc(
    alias: str,
    version: str = "1.0.0",
    *,
    inputs: list[dict[str, Any]] | None = None,
    outputs: tuple[str, ...] = ("out",),
    downstream: tuple[str, ...] = (),
    **extra: Any,
) -> dict[str, Any]:
    """A valid descriptor; ``inputs`` default to one plain float."""
    return {
        "alias": alias,
        "name": alias.replace("_", " ").title(),
        "version": version,
        "description": f"Fixture {alias}",
        "main_script": "static/main.py",
        "inputs": inputs
        if inputs is not None
        else [{"name": "x", "label": "X", "type": "float", "default": 1.0}],
        "outputs": [{"name": o, "label": o.upper(), "type": "number"} for o in outputs],
        "downstream_tools": list(downstream),
        **extra,
    }


def _wired(name: str, upstream: str, output: str) -> dict[str, Any]:
    return {
        "name": name,
        "label": name.upper(),
        "type": "float",
        "default": 1.0,
        "downstream_input": True,
        "upstream_tool": upstream,
        "upstream_output": output,
    }


@dataclass
class Env:
    src: AsyncEngine
    dst: AsyncEngine
    tmp: Path
    src_admin: int
    dst_admin: int
    kp: KeyPair
    export_svc: ExportService
    import_svc: ImportService

    async def seed_src(self, descriptor: dict[str, Any], **kw: Any) -> Tool:
        return await _seed_tool(
            self.src,
            installed_by=self.src_admin,
            bundle_root=self.tmp / "src_bundles",
            descriptor=descriptor,
            **kw,
        )

    async def seed_dst(self, descriptor: dict[str, Any], **kw: Any) -> Tool:
        return await _seed_tool(
            self.dst,
            installed_by=self.dst_admin,
            bundle_root=self.tmp / "dst_seed",
            descriptor=descriptor,
            **kw,
        )

    async def export(
        self,
        tools: list[tuple[str, str]],
        workflows: list[str] | None = None,
        *,
        include_required: bool = False,
    ) -> tuple[bytes, list[tuple[str, str]]]:
        async with session_scope(make_session_factory(self.src)) as session:
            await ensure_chain_head(session)
            selection = await self.export_svc.resolve_selection(
                session,
                tool_specs=tools,
                workflow_aliases=workflows or [],
                include_required=include_required,
            )
            data = await self.export_svc.build_bundle(
                session,
                actor_user_id=self.src_admin,
                key_pair=self.kp,
                source_install_id="src",
                tool_specs=selection.tool_specs,
                workflow_aliases=workflows or [],
            )
        return data, selection.added

    async def import_(
        self, data: bytes, *, dry_run: bool = False
    ) -> tuple[ImportResult, list[ProgressEvent]]:
        events: list[ProgressEvent] = []
        async with session_scope(make_session_factory(self.dst)) as session:
            await ensure_chain_head(session)
            result = await self.import_svc.import_bundle(
                session,
                actor_user_id=self.dst_admin,
                bundle_bytes=data,
                dry_run=dry_run,
                on_event=events.append,
            )
        return result, events

    async def dst_tool(self, alias: str) -> Tool | None:
        async with session_scope(make_session_factory(self.dst)) as session:
            return (
                await session.execute(select(Tool).where(Tool.alias == alias))
            ).scalar_one_or_none()


def _text(events: list[ProgressEvent]) -> str:
    return "\n".join(e.message for e in events)


def _outcome(result: ImportResult, alias: str) -> str:
    return next(i.outcome for i in result.items if i.alias == alias)


@pytest.fixture
async def env(
    install_pair: tuple[AsyncEngine, AsyncEngine],  # noqa: F811 — the fixture
    tmp_path: Path,
) -> Env:
    src, dst = install_pair
    src_admin = await _seed_admin(src)
    dst_admin = await _seed_admin(dst)
    writer = AuditWriter(signing_key=None, key_id=None)
    trust = make_trusted_peer_service(writer)
    kp = KeyPair.generate()
    async with session_scope(make_session_factory(dst)) as session:
        await ensure_chain_head(session)
        await trust.add_peer(
            session,
            actor_user_id=dst_admin,
            fingerprint=compute_fingerprint(kp.public_key),
            label="Source",
        )
    return Env(
        src=src,
        dst=dst,
        tmp=tmp_path,
        src_admin=src_admin,
        dst_admin=dst_admin,
        kp=kp,
        export_svc=make_export_service(writer, workflow_icons_dir=tmp_path / "src_icons"),
        import_svc=make_import_service(
            writer,
            trust_service=trust,
            bundle_root=tmp_path / "dst_bundles",
            workflow_icons_dir=tmp_path / "dst_icons",
        ),
    )


async def _seed_chain(env: Env) -> None:
    """laminate → soak → stress, linked on both sides (the showcase shape)."""
    await env.seed_src(_desc("laminate", outputs=("k", "e"), downstream=("soak", "stress")))
    await env.seed_src(
        _desc(
            "soak",
            inputs=[_wired("k", "laminate", "k")],
            outputs=("temp",),
            downstream=("stress",),
        )
    )
    await env.seed_src(
        _desc("stress", inputs=[_wired("e", "laminate", "e"), _wired("t", "soak", "temp")])
    )
    await _seed_admin_workflow(
        env.src,
        installed_by=env.src_admin,
        alias="chain",
        name="Chain",
        member_aliases=["laminate", "soak", "stress"],
    )


_CHAIN = [("laminate", "1.0.0"), ("soak", "1.0.0"), ("stress", "1.0.0")]


# ---------------------------------------------------------------------------
# Case 1 + the preview regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_of_workflow_with_its_member_tools_no_longer_fails(env: Env) -> None:
    """Regression: dry-run used to 500 (workflow-member FK after tool rollback)."""
    await _seed_chain(env)
    data, _ = await env.export(_CHAIN, ["chain"])

    result, _ = await env.import_(data, dry_run=True)

    assert result.dry_run is True
    assert [i.outcome for i in result.items] == ["imported"] * 4
    assert await env.dst_tool("laminate") is None  # nothing written
    async with session_scope(make_session_factory(env.dst)) as session:
        assert (await session.execute(select(Workflow))).scalars().all() == []
    assert not (env.tmp / "dst_bundles" / "laminate").exists()


@pytest.mark.asyncio
async def test_chain_round_trips_with_links_on_both_sides(env: Env) -> None:
    await _seed_chain(env)
    data, _ = await env.export(_CHAIN, ["chain"])

    result, events = await env.import_(data)

    assert result.imported_count == 4, _text(events)
    laminate = await env.dst_tool("laminate")
    soak = await env.dst_tool("soak")
    stress = await env.dst_tool("stress")
    assert laminate.descriptor["downstream_tools"] == ["soak", "stress"]
    assert soak.descriptor["downstream_tools"] == ["stress"]
    assert [(i["upstream_tool"], i["upstream_output"]) for i in stress.descriptor["inputs"]] == [
        ("laminate", "e"),
        ("soak", "temp"),
    ]
    assert result.link_repairs == []
    async with session_scope(make_session_factory(env.dst)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        members = (
            (
                await session.execute(
                    select(WorkflowMember.tool_alias)
                    .where(WorkflowMember.workflow_id == wf.id)
                    .order_by(WorkflowMember.position)
                )
            )
            .scalars()
            .all()
        )
        assert members == ["laminate", "soak", "stress"]
        snapshot = (
            await session.execute(select(WorkflowVersion).where(WorkflowVersion.alias == "chain"))
        ).scalar_one()
        assert snapshot.change_summary.startswith("Imported from migration bundle")
        versions = (await session.execute(select(ToolVersion))).scalars().all()
        assert versions
        assert all(v.bundle_hash for v in versions)


@pytest.mark.asyncio
async def test_reimporting_same_bundle_stays_a_noop(env: Env) -> None:
    await _seed_chain(env)
    data, _ = await env.export(_CHAIN, ["chain"])
    await env.import_(data)

    result, events = await env.import_(data)

    assert [i.outcome for i in result.items] == ["no-op"] * 4, _text(events)


# ---------------------------------------------------------------------------
# Cases 2-5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case2_existing_upstream_learns_the_new_downstream(env: Env) -> None:
    await env.seed_dst(_desc("laminate", outputs=("k",)))  # lists nothing downstream
    await env.seed_src(_desc("soak", inputs=[_wired("k", "laminate", "k")]))
    data, _ = await env.export([("soak", "1.0.0")])

    _, preview_events = await env.import_(data, dry_run=True)
    assert "Would also update tool laminate (1.0.0 → 1.0.1)" in _text(preview_events)
    assert (await env.dst_tool("laminate")).descriptor["downstream_tools"] == []

    result, _ = await env.import_(data)
    laminate = await env.dst_tool("laminate")
    assert laminate.descriptor["downstream_tools"] == ["soak"]
    assert laminate.version == "1.0.1"  # an ordinary metadata edit: patch bump
    assert result.link_repairs == [
        {
            "tool": "laminate",
            "added_downstream": "soak",
            "from_version": "1.0.0",
            "to_version": "1.0.1",
        }
    ]


@pytest.mark.asyncio
async def test_case3_missing_upstream_is_named_and_link_kept(env: Env) -> None:
    await env.seed_src(_desc("soak", inputs=[_wired("k", "laminate", "k")]))
    data, _ = await env.export([("soak", "1.0.0")])

    result, events = await env.import_(data)

    assert _outcome(result, "soak") == "imported"
    assert "wired to tool 'laminate', which this install doesn't have" in _text(events)
    assert (await env.dst_tool("soak")).descriptor["inputs"][0]["upstream_tool"] == "laminate"


@pytest.mark.asyncio
async def test_case4_imported_upstream_lists_tools_already_wired_to_it(env: Env) -> None:
    await env.seed_dst(_desc("soak", inputs=[_wired("k", "laminate", "k")]))
    await env.seed_src(_desc("laminate", "2.0.0", outputs=("k",)))
    data, _ = await env.export([("laminate", "2.0.0")])

    _, events = await env.import_(data)

    laminate = await env.dst_tool("laminate")
    assert laminate.descriptor["downstream_tools"] == ["soak"]
    assert laminate.version == "2.0.0"  # no extra bump for a tool that just arrived
    assert "also lists soak as downstream" in _text(events)


@pytest.mark.asyncio
async def test_case5_unknown_downstream_is_noted(env: Env) -> None:
    await env.seed_src(_desc("laminate", downstream=("stress",)))
    data, _ = await env.export([("laminate", "1.0.0")])

    _, events = await env.import_(data)

    assert "lists 'stress' as a downstream tool" in _text(events)
    assert (await env.dst_tool("laminate")).descriptor["downstream_tools"] == ["stress"]


@pytest.mark.asyncio
async def test_link_to_an_output_the_upstream_lacks_is_warned(env: Env) -> None:
    await env.seed_dst(_desc("laminate", outputs=("other",), downstream=("soak",)))
    await env.seed_src(_desc("soak", inputs=[_wired("k", "laminate", "k")]))
    data, _ = await env.export([("soak", "1.0.0")])

    _, events = await env.import_(data)

    assert "takes output 'k' from laminate, but laminate has no such output" in _text(events)


# ---------------------------------------------------------------------------
# Metadata, references, version rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cap_and_start_approval_travel(env: Env) -> None:
    tool = await env.seed_src(_desc("laminate"))
    async with session_scope(make_session_factory(env.src)) as session:
        row = await session.get(Tool, tool.id)
        row.run_cap = 42
        row.optimisation_start_approval_required = True
    data, _ = await env.export([("laminate", "1.0.0")])

    await env.import_(data)

    dst = await env.dst_tool("laminate")
    assert dst.run_cap == 42
    assert dst.optimisation_start_approval_required is True


@pytest.mark.asyncio
async def test_teams_partially_resolved_drop_unknown(env: Env) -> None:
    async with session_scope(make_session_factory(env.dst)) as session:
        session.add(Team(name="Thermal", description=""))
    await env.seed_src(_desc("laminate", teams=["Thermal", "Ghost"]))
    data, _ = await env.export([("laminate", "1.0.0")])

    _, events = await env.import_(data)

    dst = await env.dst_tool("laminate")
    assert dst.descriptor["teams"] == ["Thermal"]
    assert dst.teams_pending is False
    assert "'Ghost'" in _text(events)


@pytest.mark.asyncio
async def test_no_team_resolves_blocks_promotion_until_teams_are_set(env: Env) -> None:
    await env.seed_src(_desc("laminate", teams=["Ghost"]))
    data, _ = await env.export([("laminate", "1.0.0")])

    _, events = await env.import_(data)

    dst = await env.dst_tool("laminate")
    assert dst.teams_pending is True
    assert dst.descriptor["teams"] == []
    assert "imported with no teams" in _text(events)
    tools = make_tool_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(make_session_factory(env.dst)) as session:
        with pytest.raises(TeamsPendingError):
            await tools.promote_state(
                session, actor_user_id=env.dst_admin, alias="laminate", new_state="test"
            )
    async with session_scope(make_session_factory(env.dst)) as session:
        await ensure_chain_head(session)
        row = (await session.execute(select(Tool).where(Tool.alias == "laminate"))).scalar_one()
        await tools.update_descriptor(
            session,
            actor_user_id=env.dst_admin,
            descriptor=ToolDescriptor.model_validate({**row.descriptor, "teams": ["World"]}),
        )
    async with session_scope(make_session_factory(env.dst)) as session:
        await ensure_chain_head(session)
        promoted = await tools.promote_state(
            session, actor_user_id=env.dst_admin, alias="laminate", new_state="test"
        )
        assert promoted.state == "test"
        assert promoted.teams_pending is False


@pytest.mark.asyncio
async def test_approver_without_the_flag_is_dropped_and_empty_list_skips(env: Env) -> None:
    await _seed_user(env.src, "alice")
    await _seed_user(env.dst, "alice")
    async with session_scope(make_session_factory(env.dst)) as session:
        user = (await session.execute(select(User).where(User.username == "alice"))).scalar_one()
        user.is_approver = False
    await env.seed_src(_desc("laminate", approvals_policy="pre", approver_usernames=["alice"]))
    data, _ = await env.export([("laminate", "1.0.0")])

    result, events = await env.import_(data)

    assert _outcome(result, "laminate") == "skipped"
    assert "'alice' ('Can approve runs' is off)" in _text(events)


@pytest.mark.asyncio
async def test_unknown_material_class_skips_like_a_normal_install(env: Env) -> None:
    await env.seed_src(
        _desc(
            "laminate",
            inputs=[
                {"name": "m", "label": "M", "type": "material", "material_class": "Unobtanium"}
            ],
        )
    )
    data, _ = await env.export([("laminate", "1.0.0")])

    result, events = await env.import_(data)

    assert _outcome(result, "laminate") == "skipped"
    assert "Materials database" in _text(events)


@pytest.mark.asyncio
async def test_surrogate_tool_is_refused_at_export(env: Env) -> None:
    await env.seed_src(_desc("surrogate_x", surrogate_model_version_id=7))
    with pytest.raises(SurrogateToolNotExportableError):
        await env.export([("surrogate_x", "1.0.0")])


@pytest.mark.asyncio
async def test_surrogate_tool_in_an_old_bundle_is_skipped_on_import(env: Env) -> None:
    descriptor = _desc("surrogate_x", surrogate_model_version_id=7)
    data = pack_bundle(
        source_install_id="src",
        source_fingerprint=compute_fingerprint(env.kp.public_key),
        source_public_key_hex=env.kp.public_key.public_bytes_raw().hex(),
        created_by="t",
        notes="",
        tools=[
            ToolPayload(
                entry=ToolManifestEntry(
                    alias="surrogate_x", version="1.0.0", settings={"descriptor": descriptor}
                ),
                files={"static/main.py": b"x"},
            )
        ],
        workflows=[],
        signer=lambda m: sign_manifest(m, env.kp.private_key),
    )

    result, events = await env.import_(data)

    assert _outcome(result, "surrogate_x") == "skipped"
    assert "surrogate tool" in _text(events)


@pytest.mark.asyncio
async def test_older_version_is_refused_as_a_downgrade(env: Env) -> None:
    await env.seed_dst(_desc("laminate", "2.0.0"))
    await env.seed_src(_desc("laminate", "1.5.0"))
    data, _ = await env.export([("laminate", "1.5.0")])

    result, events = await env.import_(data)

    assert _outcome(result, "laminate") == "skipped"
    assert "would downgrade" in _text(events)
    assert (await env.dst_tool("laminate")).version == "2.0.0"


@pytest.mark.asyncio
async def test_newer_version_replaces_released_tool_and_warns_it_goes_offline(env: Env) -> None:
    await env.seed_dst(_desc("laminate", "1.0.0"), state="released")
    await env.seed_src(_desc("laminate", "1.1.0"))
    data, _ = await env.export([("laminate", "1.1.0")])

    _, preview_events = await env.import_(data, dry_run=True)
    assert "goes back to 'new'" in _text(preview_events)

    result, _ = await env.import_(data)
    dst = await env.dst_tool("laminate")
    assert _outcome(result, "laminate") == "imported"
    assert (dst.version, dst.state) == ("1.1.0", "new")


@pytest.mark.asyncio
async def test_archived_tool_here_is_not_overwritten(env: Env) -> None:
    tool = await env.seed_dst(_desc("laminate", "1.0.0"))
    async with session_scope(make_session_factory(env.dst)) as session:
        (await session.get(Tool, tool.id)).archived_at = _dt.datetime.now(tz=_dt.UTC)
    await env.seed_src(_desc("laminate", "1.1.0"))
    data, _ = await env.export([("laminate", "1.1.0")])

    result, events = await env.import_(data)

    assert _outcome(result, "laminate") == "skipped"
    assert "archived on this install" in _text(events)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


async def _bump_src_workflow(env: Env, alias: str, version: str, members: list[str]) -> None:
    async with session_scope(make_session_factory(env.src)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == alias))).scalar_one()
        wf.version = version
        await session.execute(
            WorkflowMember.__table__.delete().where(WorkflowMember.workflow_id == wf.id)
        )
        for i, a in enumerate(members, start=1):
            session.add(WorkflowMember(workflow_id=wf.id, tool_alias=a, position=i))


@pytest.mark.asyncio
async def test_existing_workflow_is_updated_by_a_newer_version(env: Env) -> None:
    await _seed_chain(env)
    first, _ = await env.export(_CHAIN, ["chain"])
    await env.import_(first)
    async with session_scope(make_session_factory(env.dst)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        wf.state = "released"

    await _bump_src_workflow(env, "chain", "1.1.0", ["laminate", "soak"])
    second, _ = await env.export([], ["chain"])
    result, events = await env.import_(second)

    assert _outcome(result, "chain") == "imported", _text(events)
    assert "goes back to 'new'" in _text(events)
    async with session_scope(make_session_factory(env.dst)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        assert (wf.version, wf.state) == ("1.1.0", "new")
        members = (
            (
                await session.execute(
                    select(WorkflowMember.tool_alias)
                    .where(WorkflowMember.workflow_id == wf.id)
                    .order_by(WorkflowMember.position)
                )
            )
            .scalars()
            .all()
        )
        assert members == ["laminate", "soak"]
        summaries = (
            (
                await session.execute(
                    select(WorkflowVersion.change_summary).where(WorkflowVersion.alias == "chain")
                )
            )
            .scalars()
            .all()
        )
        assert any(s.startswith("Updated from migration bundle") for s in summaries)

    again, _ = await env.import_(second)
    assert _outcome(again, "chain") == "no-op"


@pytest.mark.asyncio
async def test_workflow_same_version_different_content_or_older_is_refused(env: Env) -> None:
    await _seed_chain(env)
    data, _ = await env.export(_CHAIN, ["chain"])
    await env.import_(data)

    await _bump_src_workflow(env, "chain", "1.0.0", ["laminate", "soak"])
    conflict, _ = await env.export([], ["chain"])
    result, events = await env.import_(conflict)
    assert _outcome(result, "chain") == "skipped"
    assert "already exists here with different content" in _text(events)

    await _bump_src_workflow(env, "chain", "0.9.0", ["laminate", "soak"])
    older, _ = await env.export([], ["chain"])
    result, events = await env.import_(older)
    assert _outcome(result, "chain") == "skipped"
    assert "would downgrade" in _text(events)


@pytest.mark.asyncio
async def test_workflow_cannot_overwrite_a_users_custom_workflow(env: Env) -> None:
    await _seed_chain(env)
    async with session_scope(make_session_factory(env.dst)) as session:
        session.add(
            Workflow(
                alias="chain",
                name="Mine",
                version="1.0.0",
                kind=WorkflowKind.CUSTOM.value,
                state="released",
                descriptor={},
                installed_by_user_id=env.dst_admin,
                owner_id=env.dst_admin,
                visibility="private",
            )
        )
    data, _ = await env.export(_CHAIN, ["chain"])

    result, events = await env.import_(data)

    assert _outcome(result, "chain") == "skipped"
    assert "personal (custom) workflow" in _text(events)


@pytest.mark.asyncio
async def test_workflow_members_out_of_order_are_refused(env: Env) -> None:
    await _seed_chain(env)
    await _bump_src_workflow(env, "chain", "1.0.0", ["stress", "laminate", "soak"])
    data, _ = await env.export(_CHAIN, ["chain"])

    result, events = await env.import_(data)

    assert _outcome(result, "chain") == "skipped"
    assert "before" in _text(events)


@pytest.mark.asyncio
async def test_workflow_icon_travels(env: Env) -> None:
    await _seed_chain(env)
    async with session_scope(make_session_factory(env.src)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        wf.descriptor = {**wf.descriptor, "icon_filename": "chain.png"}
    (env.tmp / "src_icons").mkdir()
    png = b"\x89PNG\r\n\x1a\nfake-icon"
    (env.tmp / "src_icons" / "chain.png").write_bytes(png)
    data, _ = await env.export(_CHAIN, ["chain"])

    await env.import_(data)

    assert (env.tmp / "dst_icons" / "chain.png").read_bytes() == png
    async with session_scope(make_session_factory(env.dst)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        assert wf.descriptor["icon_filename"] == "chain.png"


@pytest.mark.asyncio
async def test_workflow_with_unresolved_teams_cannot_be_promoted(env: Env) -> None:
    await _seed_chain(env)
    async with session_scope(make_session_factory(env.src)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        wf.descriptor = {**wf.descriptor, "teams": ["Ghost"]}
    data, _ = await env.export(_CHAIN, ["chain"])
    await env.import_(data)

    workflows = make_workflow_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(make_session_factory(env.dst)) as session:
        wf = (await session.execute(select(Workflow).where(Workflow.alias == "chain"))).scalar_one()
        assert wf.teams_pending is True
        with pytest.raises(WorkflowStateError):
            await workflows.promote_state(
                session, actor_user_id=env.dst_admin, alias="chain", new_state="test"
            )


# ---------------------------------------------------------------------------
# Export selection, bundle format, robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_required_adds_members_and_upstream_sources(env: Env) -> None:
    await _seed_chain(env)

    _, added = await env.export([("stress", "1.0.0")], include_required=True)
    assert sorted(added) == [("laminate", "1.0.0"), ("soak", "1.0.0")]

    _, added_by_workflow = await env.export([], ["chain"], include_required=True)
    assert sorted(added_by_workflow) == sorted(_CHAIN)

    _, none_added = await env.export([("stress", "1.0.0")])
    assert none_added == []


@pytest.mark.asyncio
async def test_v1_bundle_still_imports_and_newer_format_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.seed_src(_desc("laminate"))
    monkeypatch.setattr(bundle_mod, "BUNDLE_VERSION", 1)
    v1, _ = await env.export([("laminate", "1.0.0")])
    monkeypatch.setattr(bundle_mod, "BUNDLE_VERSION", 2)
    result, _ = await env.import_(v1)
    assert _outcome(result, "laminate") == "imported"

    monkeypatch.setattr(bundle_mod, "BUNDLE_VERSION", 3)
    v3, _ = await env.export([("laminate", "1.0.0")])
    monkeypatch.setattr(bundle_mod, "BUNDLE_VERSION", 2)
    with pytest.raises(BundleError, match="newer version of MATOFX"):
        await env.import_(v3)


@pytest.mark.asyncio
async def test_unexpected_error_skips_only_that_item(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.seed_src(_desc("good"))
    await env.seed_src(_desc("bad"))
    data, _ = await env.export([("good", "1.0.0"), ("bad", "1.0.0")])
    original = ImportService._import_tool_inner

    async def flaky(self: ImportService, session: Any, *, payload: ToolPayload, **kw: Any) -> Any:
        if payload.entry.alias == "bad":
            raise RuntimeError("disk on fire")
        return await original(self, session, payload=payload, **kw)

    monkeypatch.setattr(ImportService, "_import_tool_inner", flaky)

    result, events = await env.import_(data)

    assert _outcome(result, "good") == "imported"
    assert _outcome(result, "bad") == "skipped"
    assert "unexpected error" in _text(events)
    assert "disk on fire" in _text(events)


def test_icon_cap_rejects_oversized_inline_icon() -> None:
    with pytest.raises(ValueError, match="icon exceeds"):
        bundle_mod.WorkflowManifestEntry(
            alias="w",
            name="W",
            icon_png_b64=base64.b64encode(
                b"x" * (bundle_mod.MAX_WORKFLOW_ICON_BYTES + 10)
            ).decode(),
        )
