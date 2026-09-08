"""Submit-time resolve/freeze of ``material`` inputs (ADR-0007 §3, slice 7).

``JobService._resolve_material_inputs`` expands each ``material`` field's
selected ``material_version_id`` into a frozen property block in the
job's input map and writes a ``job_material_use`` provenance row. Tested
directly (rather than through the full staging path) to keep the focus on
the freeze + provenance behaviour and reproducibility.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import make_job_service
from matofx.materials import PropertySpec, make_materials_service
from matofx.materials.orm import JobMaterialUse
from matofx.tools.descriptor import InputField, ToolDescriptor

from .conftest import Basics


def _descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        alias="heat",
        name="Heat",
        version="1.0",
        description="Uses a material.",
        main_script="m.py",
        inputs=[
            InputField(
                name="fluid",
                label="Working fluid",
                type="material",
                material_class="Fluid",
                required_properties=["thermal_conductivity"],
            )
        ],
    )


async def _published_water(factory: object, *, curator: int) -> tuple[int, int]:
    """Seed a Fluid class + a published Water material; return (material_id, version_id)."""
    audit = AuditWriter(signing_key=None, key_id=None)
    materials = make_materials_service(audit)
    async with session_scope(factory) as session:  # type: ignore[arg-type]
        cls = await materials.create_class(session, actor_user_id=curator, name="Fluid")
        await materials.add_class_property(
            session,
            actor_user_id=curator,
            class_id=cls.id,
            prop_name="thermal_conductivity",
            unit="W/m/K",
            required=True,
        )
        material = await materials.create_material(
            session,
            actor_user_id=curator,
            name="Water",
            class_id=cls.id,
            provenance="handbook",
            approval_required=False,
            properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
        )
        material_id = material.id
        version = (await materials.list_versions(session, material_id))[0]
        return material_id, version.id


@pytest.mark.asyncio
async def test_resolve_freezes_block_and_writes_provenance(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(materials_engine)
    material_id, version_id = await _published_water(factory, curator=basics.user_id)
    jobs = make_job_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        resolved = await jobs._resolve_material_inputs(
            session, basics.job_id, _descriptor(), {"fluid": version_id}
        )
        block = resolved["fluid"]
        assert block["material_id"] == material_id
        assert block["class"] == "Fluid"
        assert block["version_id"] == version_id
        # Frozen block is keyed by slug; value carries prop_name display label.
        assert block["properties"]["thermal_conductivity"] == {
            "prop_name": "thermal_conductivity",
            "value": 0.6,
            "unit": "W/m/K",
        }

    async with session_scope(factory) as session:
        use = await session.get(JobMaterialUse, (basics.job_id, "fluid"))
        assert use is not None
        assert use.material_version_id == version_id


@pytest.mark.asyncio
async def test_resolve_accepts_string_version_id(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    """The UI material picker stores the version id as a string ("5")."""
    factory = make_session_factory(materials_engine)
    material_id, version_id = await _published_water(factory, curator=basics.user_id)
    jobs = make_job_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        resolved = await jobs._resolve_material_inputs(
            session, basics.job_id, _descriptor(), {"fluid": str(version_id)}
        )
        assert resolved["fluid"]["material_id"] == material_id
        assert resolved["fluid"]["version_id"] == version_id

    async with session_scope(factory) as session:
        use = await session.get(JobMaterialUse, (basics.job_id, "fluid"))
        assert use is not None
        assert use.material_version_id == version_id


@pytest.mark.asyncio
async def test_resolve_is_reproducible_on_requeue(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    """A re-queue sees an already-frozen block: kept verbatim, provenance re-recorded."""
    factory = make_session_factory(materials_engine)
    material_id, version_id = await _published_water(factory, curator=basics.user_id)
    jobs = make_job_service(AuditWriter(signing_key=None, key_id=None))
    frozen = {
        "material_id": material_id,
        "name": "Water",
        "class": "Fluid",
        "version_id": version_id,
        "version_ordinal": 1,
        "properties": {"thermal_conductivity": {"value": 0.6, "unit": "W/m/K"}},
    }
    async with session_scope(factory) as session:
        resolved = await jobs._resolve_material_inputs(
            session, basics.job_id, _descriptor(), {"fluid": frozen}
        )
        assert resolved["fluid"] == frozen  # unchanged → reproducible
    async with session_scope(factory) as session:
        use = await session.get(JobMaterialUse, (basics.job_id, "fluid"))
        assert use is not None
        assert use.material_version_id == version_id
