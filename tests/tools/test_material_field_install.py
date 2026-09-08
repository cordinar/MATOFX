"""Install-time validation of ``material`` inputs against the class schema (ADR-0007 §3).

The descriptor model guarantees a ``material`` field declares a
``material_class``; :class:`ToolService` resolves that class (and any
explicit ``required_properties``) against the live materials schema at
install/update so a mismatch surfaces before any job runs.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from matofx.db import make_session_factory, session_scope
from matofx.materials.orm import MaterialClass, MaterialClassProperty
from matofx.tools import (
    InputField,
    ToolDescriptor,
    ToolService,
    UnknownMaterialClassError,
    UnknownMaterialPropertyError,
)


async def _seed_fluid_class(
    factory: async_sessionmaker[AsyncSession], *, state: str = "active"
) -> None:
    """Seed a 'Fluid' class (active by default) requiring thermal_conductivity."""
    async with session_scope(factory) as session:
        cls = MaterialClass(name="Fluid", description="", state=state)
        session.add(cls)
        await session.flush()
        session.add(
            MaterialClassProperty(
                class_id=cls.id,
                slug="thermal_conductivity",
                prop_name="thermal_conductivity",
                unit="W/m/K",
                required=True,
            )
        )


def _descriptor_with_material(required_properties: list[str] | None = None) -> ToolDescriptor:
    return ToolDescriptor(
        alias="heat-tool",
        name="Heat Tool",
        version="1.0.0",
        description="Uses a material.",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="fluid",
                label="Working fluid",
                type="material",
                material_class="Fluid",
                required_properties=required_properties,
            )
        ],
    )


@pytest.mark.asyncio
async def test_install_with_material_field_resolves_class(
    tools_engine: AsyncEngine, tool_service: ToolService, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    await _seed_fluid_class(factory)
    async with session_scope(factory) as session:
        tool = await tool_service.install(
            session,
            actor_user_id=seed_roles["super_user"],
            descriptor=_descriptor_with_material(required_properties=["thermal_conductivity"]),
            bundle_path="/tools/heat",
        )
    assert tool.alias == "heat-tool"


@pytest.mark.asyncio
async def test_install_unknown_material_class_rejected(
    tools_engine: AsyncEngine, tool_service: ToolService, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    # no material class seeded → the field can't resolve
    async with session_scope(factory) as session:
        with pytest.raises(UnknownMaterialClassError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor_with_material(),
                bundle_path="/tools/heat",
            )


@pytest.mark.asyncio
async def test_install_retired_material_class_rejected(
    tools_engine: AsyncEngine, tool_service: ToolService, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    await _seed_fluid_class(factory, state="retired")
    async with session_scope(factory) as session:
        with pytest.raises(UnknownMaterialClassError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor_with_material(),
                bundle_path="/tools/heat",
            )


@pytest.mark.asyncio
async def test_install_unknown_required_property_rejected(
    tools_engine: AsyncEngine, tool_service: ToolService, seed_roles: dict[str, int]
) -> None:
    factory = make_session_factory(tools_engine)
    await _seed_fluid_class(factory)
    async with session_scope(factory) as session:
        with pytest.raises(UnknownMaterialPropertyError):
            await tool_service.install(
                session,
                actor_user_id=seed_roles["super_user"],
                descriptor=_descriptor_with_material(required_properties=["specific_heat"]),
                bundle_path="/tools/heat",
            )
