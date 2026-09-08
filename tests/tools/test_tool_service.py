"""End-to-end tests for ToolService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.tools import (
    DuplicateToolError,
    InvalidStateTransitionError,
    ToolDescriptor,
    ToolNotFoundError,
    ToolPermissionError,
    ToolService,
)
from matofx.tools.errors import PythonEnvNotUsableError
from matofx.tools.exec_orm import PythonEnv


@pytest.mark.asyncio
async def test_install_creates_tool_in_new_state(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        tool = await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
    assert tool.alias == "demo-tool"
    assert tool.state == "new"
    assert tool.bundle_path == "/tools/demo"


@pytest.mark.asyncio
async def test_administrator_can_install(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        tool = await tool_service.install(
            session,
            actor_user_id=seed_roles["administrator"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
    assert tool.state == "new"


@pytest.mark.asyncio
async def test_regular_user_cannot_install(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(ToolPermissionError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["regular_user"],
                descriptor=example_descriptor,
                bundle_path="/tools/demo",
            )


@pytest.mark.asyncio
async def test_beta_tester_cannot_install(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        with pytest.raises(ToolPermissionError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["beta_tester"],
                descriptor=example_descriptor,
                bundle_path="/tools/demo",
            )


@pytest.mark.asyncio
async def test_duplicate_alias_rejected(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateToolError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=example_descriptor,
                bundle_path="/tools/demo2",
            )


@pytest.mark.asyncio
async def test_promote_through_states(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )

    async with session_scope(factory) as session:
        tool = await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="test",
        )
    assert tool.state == "test"

    async with session_scope(factory) as session:
        tool = await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="released",
        )
    assert tool.state == "released"


@pytest.mark.asyncio
async def test_rollback_to_test_allowed(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    """Phase 1 allows rolling a release back when something's broken."""
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
        await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="test",
        )
        await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="released",
        )
    async with session_scope(factory) as session:
        tool = await tool_service.promote_state(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            new_state="test",
        )
    assert tool.state == "test"


@pytest.mark.asyncio
async def test_invalid_state_target_rejected(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidStateTransitionError):
            await tool_service.promote_state(
                session,
                actor_user_id=seed_roles["super_user"],
                alias="demo-tool",
                new_state="exploded",
            )


@pytest.mark.asyncio
async def test_regular_user_cannot_promote(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
    async with session_scope(factory) as session:
        with pytest.raises(ToolPermissionError):
            await tool_service.promote_state(
                session,
                actor_user_id=seed_roles["regular_user"],
                alias="demo-tool",
                new_state="released",
            )


@pytest.mark.asyncio
async def test_visibility_filtering(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    seed_roles: dict[str, int],
) -> None:
    """Each role sees the right slice of the tool catalog."""
    factory = make_session_factory(tools_engine)

    async def _install_at(alias: str, target_state: str) -> None:
        # Install lands in 'new'; walk the forward graph to reach target.
        descriptor = ToolDescriptor(
            alias=alias,
            name=alias,
            version="1.0.0",
            description="x",
            main_script="static/m.py",
        )
        async with session_scope(factory) as session:
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=descriptor,
                bundle_path=f"/tools/{alias}",
            )
            if target_state in {"test", "released"}:
                await tool_service.promote_state(
                    session,
                    actor_user_id=seed_roles["super_user"],
                    alias=alias,
                    new_state="test",
                )
            if target_state == "released":
                await tool_service.promote_state(
                    session,
                    actor_user_id=seed_roles["super_user"],
                    alias=alias,
                    new_state="released",
                )

    await _install_at("alpha", "new")
    await _install_at("beta", "test")
    await _install_at("gamma", "released")

    async def _aliases_visible_to(user_id: int) -> set[str]:
        async with session_scope(factory) as session:
            tools = await tool_service.list_visible(session, actor_user_id=user_id)
        return {t.alias for t in tools}

    assert await _aliases_visible_to(seed_roles["super_user"]) == {"alpha", "beta", "gamma"}
    assert await _aliases_visible_to(seed_roles["administrator"]) == {
        "alpha",
        "beta",
        "gamma",
    }
    assert await _aliases_visible_to(seed_roles["beta_tester"]) == {"beta", "gamma"}
    assert await _aliases_visible_to(seed_roles["regular_user"]) == {"gamma"}


@pytest.mark.asyncio
async def test_get_hides_invisible_tools(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    """A regular user trying to fetch a 'new' tool gets ToolNotFoundError."""
    factory = make_session_factory(tools_engine)
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=example_descriptor,
            bundle_path="/tools/demo",
        )
    async with session_scope(factory) as session:
        with pytest.raises(ToolNotFoundError):
            await tool_service.get(
                session, actor_user_id=seed_roles["regular_user"], alias="demo-tool"
            )


# --- set_python_env (row-level execution-env pin) ---------------------------


async def _make_python_env(
    factory: object,
    *,
    name: str,
    interpreter_path: str = "/opt/envs/py/bin/python",
    is_active: bool = True,
) -> int:
    """Insert a python_env row directly and return its id."""
    async with session_scope(factory) as session:  # type: ignore[arg-type]
        env = PythonEnv(name=name, interpreter_path=interpreter_path, is_active=is_active)
        session.add(env)
        await session.flush()
        return int(env.id)


async def _install_demo(
    factory: object, tool_service: ToolService, descriptor: ToolDescriptor, actor_id: int
) -> None:
    async with session_scope(factory) as session:  # type: ignore[arg-type]
        await tool_service.install(
            session, actor_user_id=actor_id, descriptor=descriptor, bundle_path="/tools/demo"
        )


@pytest.mark.asyncio
async def test_set_python_env_pins_active_env(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    """Pinning a tool to an active env stores the id; clearing resets to None."""
    factory = make_session_factory(tools_engine)
    await _install_demo(factory, tool_service, example_descriptor, seed_roles["super_user"])
    env_id = await _make_python_env(factory, name="py312")

    async with session_scope(factory) as session:
        tool = await tool_service.set_python_env(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            python_env_id=env_id,
        )
        assert tool.python_env_id == env_id

    # Clearing the pin returns to the default (None → main MATOFX env).
    async with session_scope(factory) as session:
        tool = await tool_service.set_python_env(
            session,
            actor_user_id=seed_roles["super_user"],
            alias="demo-tool",
            python_env_id=None,
        )
        assert tool.python_env_id is None


@pytest.mark.asyncio
async def test_set_python_env_rejects_inactive_env(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    """A deactivated env can't be pinned — new bindings to it are forbidden."""
    factory = make_session_factory(tools_engine)
    await _install_demo(factory, tool_service, example_descriptor, seed_roles["super_user"])
    env_id = await _make_python_env(factory, name="retired", is_active=False)

    async with session_scope(factory) as session:
        with pytest.raises(PythonEnvNotUsableError):
            await tool_service.set_python_env(
                session,
                actor_user_id=seed_roles["super_user"],
                alias="demo-tool",
                python_env_id=env_id,
            )


@pytest.mark.asyncio
async def test_set_python_env_rejects_unknown_env(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    """An id with no matching python_env row is rejected."""
    factory = make_session_factory(tools_engine)
    await _install_demo(factory, tool_service, example_descriptor, seed_roles["super_user"])

    async with session_scope(factory) as session:
        with pytest.raises(PythonEnvNotUsableError):
            await tool_service.set_python_env(
                session,
                actor_user_id=seed_roles["super_user"],
                alias="demo-tool",
                python_env_id=99999,
            )


@pytest.mark.asyncio
async def test_set_python_env_requires_install_permission(
    tools_engine: AsyncEngine,
    tool_service: ToolService,
    example_descriptor: ToolDescriptor,
    seed_roles: dict[str, int],
) -> None:
    """Regular users can't change a tool's execution env."""
    factory = make_session_factory(tools_engine)
    await _install_demo(factory, tool_service, example_descriptor, seed_roles["super_user"])
    env_id = await _make_python_env(factory, name="py312")

    async with session_scope(factory) as session:
        with pytest.raises(ToolPermissionError):
            await tool_service.set_python_env(
                session,
                actor_user_id=seed_roles["regular_user"],
                alias="demo-tool",
                python_env_id=env_id,
            )
