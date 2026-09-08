"""``list_visible`` merges the caller's own custom workflows.

The Workflow Quick Access card and the Add-Job(s) chooser both read
``WorkflowService.list_visible``. It returns admin workflows the caller
may see **plus** the caller's own custom workflows (owned + shared) — the
same set as the ``/my-workflows`` page.

Regression guard: a custom workflow must reach *only* its owner and its
share recipients here. Custom rows are ``released`` + un-teamed, so an
earlier implementation that ran them through the admin state/team gate
leaked every custom workflow to every user; the over-correction that
followed dropped all custom rows, hiding a user's own workflows from
Quick Access and the Add-Job(s) chooser. Neither extreme is correct.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.tools import ToolService
from matofx.workflows import WorkflowService
from matofx.workflows.service import CustomWorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor


async def _visible_aliases(
    factory, workflow_service: WorkflowService, *, actor_user_id: int
) -> set[str]:
    async with session_scope(factory) as session:
        rows = await workflow_service.list_visible(session, actor_user_id=actor_user_id)
        return {wf.alias for wf in rows}


@pytest.mark.asyncio
async def test_owner_sees_own_custom_workflow_others_do_not(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A private custom workflow reaches its owner via ``list_visible``
    but not another regular user — and not an inspector (admin) who
    didn't author it."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]  # holds custom_workflows.inspect
    owner_id = seed_roles["regular_user"]
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
                name="My private flow",
                description="",
                tool_aliases=["alpha", "beta"],
            ),
        )
        wf_alias = wf.alias

    # Owner sees it.
    assert wf_alias in await _visible_aliases(factory, workflow_service, actor_user_id=owner_id)
    # Another regular user does not.
    assert wf_alias not in await _visible_aliases(factory, workflow_service, actor_user_id=other_id)
    # An inspector (admin) does NOT get every custom workflow here — the
    # inspect grant is for the admin inspector page, not Quick Access.
    assert wf_alias not in await _visible_aliases(factory, workflow_service, actor_user_id=admin_id)


@pytest.mark.asyncio
async def test_share_recipient_sees_shared_custom_workflow(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
) -> None:
    """A custom workflow shared with a user reaches that recipient via
    ``list_visible``."""
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    owner_id = seed_roles["regular_user"]
    recipient_id = seed_roles["beta_tester"]

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
                name="Shared flow",
                description="",
                tool_aliases=["alpha", "beta"],
                share_with_user_ids=[recipient_id],
            ),
        )
        wf_alias = wf.alias

    assert wf_alias in await _visible_aliases(factory, workflow_service, actor_user_id=recipient_id)
