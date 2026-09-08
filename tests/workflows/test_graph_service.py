"""Tests for :class:`matofx.workflows.graph_service.WorkflowGraphService`.

Covers:

- Structure-only build for an admin workflow (nodes ordered by
  position; deep_link points at the tool admin surface).
- Per-edge field-name capture from ``downstream_input`` declarations.
- Floating-node case (chosen tool with no descriptor-declared wiring
  to another chosen tool) — empty edges, node still present.
- Visibility gate hiding a non-shared custom workflow from another
  user — existence-hide via :class:`WorkflowNotFoundError`.
- Run-overlay: per-node ``run_state`` mapping from child Job rows,
  including the ``waiting`` vs ``ready`` distinction.
- Cross-workflow run id rejected via :class:`NotFoundError`.

The run-overlay tests build the WorkflowRun + child Job rows directly
via the ORM (rather than going through :meth:`WorkflowRunService.start_run`)
so the test focuses on the graph service's projection logic, not on
the run-creation pipeline.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools import ToolService
from matofx.tools.orm import ToolVersion
from matofx.workflows import (
    WorkflowGraphService,
    WorkflowRun,
    WorkflowService,
    make_workflow_graph_service,
)
from matofx.workflows.errors import (
    DuplicateMemberError,
    UnknownToolMemberError,
    WorkflowNotFoundError,
)
from matofx.workflows.graph_types import WorkflowNodeRunState
from matofx.workflows.service import CustomWorkflowDraft, WorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor


@pytest.fixture
def graph_service(audit_writer: AuditWriter) -> WorkflowGraphService:
    return make_workflow_graph_service(audit_writer)


def _consumes(upstream: str, *, field_name: str = "input_1") -> list[dict]:
    """Descriptor inputs stub: one ``downstream_input`` from ``upstream``."""
    return [
        {
            "name": field_name,
            "label": field_name.replace("_", " ").title(),
            "type": "float",
            "downstream_input": True,
            "upstream_tool": upstream,
        }
    ]


# ---- structure-only -----------------------------------------------------


@pytest.mark.asyncio
async def test_build_structure_only_chain(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """A → B → C admin workflow: nodes in position order, two edges, no run state."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    descriptors = [
        make_tool_descriptor(alias="alpha", downstream_tools=["beta"], teams=["World"]),
        make_tool_descriptor(alias="beta", downstream_tools=["gamma"], teams=["World"]),
        make_tool_descriptor(alias="gamma", teams=["World"]),
    ]
    for d in descriptors:
        await install_released_tool(factory, tool_service, actor_user_id=admin_id, descriptor=d)

    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="chain_wf",
                name="Chain",
                description="A → B → C",
                tool_aliases=["alpha", "beta", "gamma"],
                teams=["World"],
            ),
        )
        wf_id = wf.id

    async with session_scope(factory) as session:
        graph = await graph_service.build(session, workflow_id=wf_id, actor_user_id=admin_id)

    assert graph.workflow_id == wf_id
    assert graph.workflow_alias == "chain_wf"
    assert graph.run_id is None
    assert graph.run_state is None
    # Nodes in position order, labels prefixed with the position.
    assert [n.tool_alias for n in graph.nodes] == ["alpha", "beta", "gamma"]
    assert [n.position for n in graph.nodes] == [1, 2, 3]
    assert all(n.run_state is None for n in graph.nodes)
    assert all(n.child_job_id is None for n in graph.nodes)
    assert all(n.deep_link == f"/admin/tools/{n.tool_alias}/edit" for n in graph.nodes)
    # Two forward edges, alpha→beta and beta→gamma. Sorted output.
    assert [(e.source, e.target) for e in graph.edges] == [
        ("alpha", "beta"),
        ("beta", "gamma"),
    ]
    # Forward-only edges have empty field_labels.
    assert all(e.field_labels == () for e in graph.edges)


@pytest.mark.asyncio
async def test_build_per_field_edge_labels(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """``downstream_input`` declarations attach the descriptor's ``label`` to the matching edge.

    The label is the human-facing text the user sees on the submit
    form (``"Initial temperature"`` rather than the wire name
    ``"temperature"``); the graph view exposes labels so the user
    reads the same word in both places.
    """
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="src", teams=["World"]),
    )
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(
            alias="dst",
            inputs=_consumes("src", field_name="temperature"),
            teams=["World"],
        ),
    )

    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="paired_wf",
                name="Paired",
                description="src feeds dst.temperature",
                tool_aliases=["src", "dst"],
                teams=["World"],
            ),
        )
        wf_id = wf.id

    async with session_scope(factory) as session:
        graph = await graph_service.build(session, workflow_id=wf_id, actor_user_id=admin_id)

    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert (edge.source, edge.target) == ("src", "dst")
    # ``_consumes`` stub sets ``label`` to a Title-Case version of the
    # name — the edge carries that label, not the wire name.
    assert edge.field_labels == ("Temperature",)


@pytest.mark.asyncio
async def test_build_with_floating_node(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """Two unrelated tools — both nodes present, no edges."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    for alias in ("solo_a", "solo_b"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="solo_wf",
                name="Solos",
                description="Two unrelated tools.",
                tool_aliases=["solo_a", "solo_b"],
                teams=["World"],
            ),
        )
        wf_id = wf.id

    async with session_scope(factory) as session:
        graph = await graph_service.build(session, workflow_id=wf_id, actor_user_id=admin_id)

    assert [n.tool_alias for n in graph.nodes] == ["solo_a", "solo_b"]
    assert graph.edges == ()


# ---- visibility gate ----------------------------------------------------


@pytest.mark.asyncio
async def test_build_hides_unowned_private_custom_workflow(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """Another regular user can't see a private custom workflow — 404."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    # The beta-tester role exists in seed_roles too — use it as the "other" user.
    other_id = seed_roles["beta_tester"]

    for alias in ("alpha", "beta"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    async with session_scope(factory) as session:
        wf = await workflow_service.create_custom(
            session,
            actor_user_id=owner_id,
            draft=CustomWorkflowDraft(
                name="Mine only",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
        wf_id = wf.id

    # Owner can build the graph.
    async with session_scope(factory) as session:
        owner_graph = await graph_service.build(session, workflow_id=wf_id, actor_user_id=owner_id)
    assert [n.tool_alias for n in owner_graph.nodes] == ["alpha", "beta"]

    # The other user is existence-hidden — same shape as a missing id.
    async with session_scope(factory) as session:
        with pytest.raises(WorkflowNotFoundError):
            await graph_service.build(session, workflow_id=wf_id, actor_user_id=other_id)


# ---- run overlay --------------------------------------------------------


async def _seed_project_scenario(
    factory,
    *,
    owner_id: int,
    name: str = "graph-test-proj",
) -> tuple[int, int]:
    """Insert a project + scenario the owner can VIEW.

    Creator gets OWNER-equivalent access via :attr:`Project.creator_user_id`
    (ADR-0002 §3) — no separate ACL row is required for the owner to
    pass the VIEW gate.
    """
    async with session_scope(factory) as session:
        project = Project(
            name=name,
            description="",
            state="active",
            creator_user_id=owner_id,
        )
        session.add(project)
        await session.flush()
        scenario = Scenario(
            project_id=project.id,
            name="graph-test-scn",
            state="active",
        )
        session.add(scenario)
        await session.flush()
        return int(project.id), int(scenario.id)


async def _seed_run_with_children(
    factory,
    *,
    workflow_id: int,
    workflow_version: str,
    scenario_id: int,
    owner_id: int,
    member_aliases: list[str],
    child_states: dict[str, str],
) -> int:
    """Insert a WorkflowRun + one child Job per member alias.

    ``child_states`` overrides the per-alias state; defaults to
    ``"created"`` for any alias not listed.
    """
    async with session_scope(factory) as session:
        # Resolve a tool_version per alias (any version row works for the FK).
        version_id_by_alias: dict[str, int] = {}
        for alias in set(member_aliases):
            stmt = (
                select(ToolVersion.id)
                .where(ToolVersion.alias == alias)
                .order_by(ToolVersion.created_at.desc())
                .limit(1)
            )
            version_id_by_alias[alias] = int((await session.execute(stmt)).scalar_one())
        run = WorkflowRun(
            workflow_id=workflow_id,
            workflow_version_snapshot=workflow_version,
            scenario_id=scenario_id,
            name="overlay test run",
            submitted_by_user_id=owner_id,
        )
        session.add(run)
        await session.flush()
        for position, alias in enumerate(member_aliases, start=1):
            session.add(
                Job(
                    scenario_id=scenario_id,
                    workflow_run_id=run.id,
                    workflow_position=position,
                    name=f"{alias}-child",
                    tool_alias=alias,
                    tool_version_id=version_id_by_alias[alias],
                    state=child_states.get(alias, "created"),
                    inputs={},
                    created_by_user_id=owner_id,
                )
            )
        await session.flush()
        return int(run.id)


@pytest.mark.asyncio
async def test_build_with_run_id_overlays_states(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """Run-mode build maps child Job states onto per-node ``run_state``."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    descriptors = [
        make_tool_descriptor(alias="a1", teams=["World"]),
        make_tool_descriptor(
            alias="b1",
            inputs=_consumes("a1", field_name="seed"),
            teams=["World"],
        ),
        make_tool_descriptor(alias="c1", teams=["World"]),
    ]
    for d in descriptors:
        await install_released_tool(factory, tool_service, actor_user_id=admin_id, descriptor=d)

    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="overlay_wf",
                name="Overlay",
                description="",
                tool_aliases=["a1", "b1", "c1"],
                teams=["World"],
            ),
        )
        wf_id = wf.id
        wf_version = wf.version

    _project_id, scenario_id = await _seed_project_scenario(factory, owner_id=admin_id)
    run_id = await _seed_run_with_children(
        factory,
        workflow_id=wf_id,
        workflow_version=wf_version,
        scenario_id=scenario_id,
        owner_id=admin_id,
        member_aliases=["a1", "b1", "c1"],
        child_states={
            "a1": "complete",
            "b1": "created",  # upstream a1 complete → ready
            "c1": "created",  # no upstream wiring within the workflow → ready
        },
    )

    async with session_scope(factory) as session:
        graph = await graph_service.build(
            session, workflow_id=wf_id, actor_user_id=admin_id, run_id=run_id
        )

    assert graph.run_id == run_id
    # compute_state: no queued/running and not all complete → "draft"
    # ("draft" means no in-flight children right now, not "no progress").
    assert graph.run_state == "draft"
    by_alias = {n.tool_alias: n for n in graph.nodes}
    assert by_alias["a1"].run_state == WorkflowNodeRunState.COMPLETE
    assert by_alias["b1"].run_state == WorkflowNodeRunState.READY
    # c1 has no descriptor-declared upstream within the workflow → ready.
    assert by_alias["c1"].run_state == WorkflowNodeRunState.READY
    # Run-mode deep_link points at the child Job, not the tool admin page.
    for node in graph.nodes:
        assert node.child_job_id is not None
        assert node.deep_link == f"/jobs/{node.child_job_id}"


@pytest.mark.asyncio
async def test_build_with_run_id_waiting_when_upstream_not_complete(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """A child whose upstream is still running shows ``waiting``."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    descriptors = [
        make_tool_descriptor(alias="up1", teams=["World"]),
        make_tool_descriptor(
            alias="down1",
            inputs=_consumes("up1", field_name="dep"),
            teams=["World"],
        ),
    ]
    for d in descriptors:
        await install_released_tool(factory, tool_service, actor_user_id=admin_id, descriptor=d)

    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="wait_wf",
                name="Wait",
                description="",
                tool_aliases=["up1", "down1"],
                teams=["World"],
            ),
        )
        wf_id = wf.id
        wf_version = wf.version

    _project_id, scenario_id = await _seed_project_scenario(
        factory, owner_id=admin_id, name="wait-test"
    )
    run_id = await _seed_run_with_children(
        factory,
        workflow_id=wf_id,
        workflow_version=wf_version,
        scenario_id=scenario_id,
        owner_id=admin_id,
        member_aliases=["up1", "down1"],
        child_states={
            "up1": "running",
            "down1": "created",  # upstream still running → waiting
        },
    )

    async with session_scope(factory) as session:
        graph = await graph_service.build(
            session, workflow_id=wf_id, actor_user_id=admin_id, run_id=run_id
        )

    by_alias = {n.tool_alias: n for n in graph.nodes}
    assert by_alias["up1"].run_state == WorkflowNodeRunState.RUNNING
    assert by_alias["down1"].run_state == WorkflowNodeRunState.WAITING


# ---- preview (unsaved tool selection) -----------------------------------


@pytest.mark.asyncio
async def test_build_preview_projects_descriptor_chain(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """Preview over a 3-tool chain projects the same edges + labels as a saved workflow."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    descriptors = [
        make_tool_descriptor(alias="p_a", teams=["World"]),
        make_tool_descriptor(
            alias="p_b",
            inputs=_consumes("p_a", field_name="result"),
            teams=["World"],
        ),
        make_tool_descriptor(alias="p_c", teams=["World"]),
    ]
    for d in descriptors:
        await install_released_tool(factory, tool_service, actor_user_id=admin_id, descriptor=d)

    async with session_scope(factory) as session:
        graph = await graph_service.build_preview(
            session,
            tool_aliases=["p_a", "p_b", "p_c"],
            actor_user_id=admin_id,
        )

    # Sentinel header values — preview, not a real workflow row.
    assert graph.workflow_id == 0
    assert graph.workflow_name == "Preview"
    assert graph.run_id is None
    # Nodes preserve user-supplied order with 1-based positions.
    assert [n.tool_alias for n in graph.nodes] == ["p_a", "p_b", "p_c"]
    assert [n.position for n in graph.nodes] == [1, 2, 3]
    assert all(n.run_state is None for n in graph.nodes)
    assert all(n.deep_link == f"/admin/tools/{n.tool_alias}/edit" for n in graph.nodes)
    # One backward edge from p_a→p_b carrying the descriptor label.
    edge_pairs = [(e.source, e.target) for e in graph.edges]
    assert ("p_a", "p_b") in edge_pairs
    p_a_to_p_b = next(e for e in graph.edges if (e.source, e.target) == ("p_a", "p_b"))
    assert p_a_to_p_b.field_labels == ("Result",)


@pytest.mark.asyncio
async def test_build_preview_rejects_unknown_alias(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """An alias the DB doesn't know about → UnknownToolMemberError."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="known_one", teams=["World"]),
    )
    async with session_scope(factory) as session:
        with pytest.raises(UnknownToolMemberError):
            await graph_service.build_preview(
                session,
                tool_aliases=["known_one", "ghost_two"],
                actor_user_id=admin_id,
            )


@pytest.mark.asyncio
async def test_build_preview_rejects_duplicate_alias(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """Same alias twice → DuplicateMemberError (matches save-path semantics)."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    await install_released_tool(
        factory,
        tool_service,
        actor_user_id=admin_id,
        descriptor=make_tool_descriptor(alias="solo", teams=["World"]),
    )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateMemberError):
            await graph_service.build_preview(
                session,
                tool_aliases=["solo", "solo"],
                actor_user_id=admin_id,
            )


@pytest.mark.asyncio
async def test_build_with_run_id_for_different_workflow_existence_hides(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    graph_service: WorkflowGraphService,
    seed_roles: dict[str, int],
) -> None:
    """A run belonging to workflow A can't be overlaid onto workflow B's graph."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]

    for alias in ("x1", "y1"):
        await install_released_tool(
            factory,
            tool_service,
            actor_user_id=admin_id,
            descriptor=make_tool_descriptor(alias=alias, teams=["World"]),
        )

    async with session_scope(factory) as session:
        wf_a = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="wf_a",
                name="A",
                description="",
                tool_aliases=["x1", "y1"],
                teams=["World"],
            ),
        )
        wf_b = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="wf_b",
                name="B",
                description="",
                tool_aliases=["x1", "y1"],
                teams=["World"],
            ),
        )
        wf_a_id = wf_a.id
        wf_a_version = wf_a.version
        wf_b_id = wf_b.id

    _project_id, scenario_id = await _seed_project_scenario(
        factory, owner_id=admin_id, name="mismatch-test"
    )
    run_id = await _seed_run_with_children(
        factory,
        workflow_id=wf_a_id,
        workflow_version=wf_a_version,
        scenario_id=scenario_id,
        owner_id=admin_id,
        member_aliases=["x1", "y1"],
        child_states={},
    )

    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await graph_service.build(
                session,
                workflow_id=wf_b_id,
                actor_user_id=admin_id,
                run_id=run_id,
            )
