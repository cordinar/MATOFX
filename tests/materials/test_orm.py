"""ORM + constraint tests for the materials data model (slice 1).

Schema correctness — the closed-vocabulary CHECKs (class/material state,
version publish state, data pedigree), the unique schema/ordinal/property
constraints, the deliberately non-unique material name, and the
provenance composite PK. Migration ``0048`` round-trips on both engines
via ``tests/db/test_alembic.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from matofx.db import make_session_factory, session_scope
from matofx.materials import (
    JobMaterialUse,
    Material,
    MaterialClass,
    MaterialClassProperty,
    MaterialProperty,
    MaterialVersion,
)

from .conftest import Basics


async def _new_class(session: AsyncSession, **overrides: object) -> MaterialClass:
    fields: dict[str, object] = {"name": "Fluid", "description": "liquids + gases"}
    fields.update(overrides)
    mc = MaterialClass(**fields)
    session.add(mc)
    await session.flush()
    return mc


def _material(class_id: int, basics: Basics, **overrides: object) -> Material:
    fields: dict[str, object] = {
        "name": "Water",
        "class_id": class_id,
        "description": "H2O",
        "steward_user_id": basics.user_id,
        "created_by_user_id": basics.user_id,
    }
    fields.update(overrides)
    return Material(**fields)


def _version(
    material_id: int, basics: Basics, *, ordinal: int = 1, **overrides: object
) -> MaterialVersion:
    fields: dict[str, object] = {
        "material_id": material_id,
        "ordinal": ordinal,
        "provenance": "handbook",
        "created_by_user_id": basics.user_id,
    }
    fields.update(overrides)
    return MaterialVersion(**fields)


@pytest.mark.asyncio
async def test_create_class_material_version_and_properties(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    """A class with an expected-property schema, a material, a version, props."""
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        session.add(
            MaterialClassProperty(
                class_id=mc.id,
                slug="thermal_conductivity",
                prop_name="thermal_conductivity",
                unit="W/m/K",
                required=True,
            )
        )
        mat = _material(mc.id, basics, aliases=["dihydrogen monoxide"], tags=["coolant"])
        session.add(mat)
        await session.flush()
        v = _version(mat.id, basics, data_pedigree="measured")
        session.add(v)
        await session.flush()
        session.add(
            MaterialProperty(
                material_version_id=v.id,
                slug="thermal_conductivity",
                prop_name="thermal_conductivity",
                value=0.6,
                unit="W/m/K",
            )
        )
        await session.flush()
        material_id, version_id = mat.id, v.id

    async with session_scope(factory) as session:
        loaded_mat = await session.get(Material, material_id)
        assert loaded_mat is not None
        assert loaded_mat.state == "active"  # default
        assert loaded_mat.version == 1  # optimistic-lock default
        assert loaded_mat.aliases == ["dihydrogen monoxide"]
        assert loaded_mat.tags == ["coolant"]
        loaded_v = await session.get(MaterialVersion, version_id)
        assert loaded_v is not None
        assert loaded_v.ordinal == 1
        assert loaded_v.publish_state == "draft"  # default
        assert loaded_v.data_pedigree == "measured"
        props = (
            (
                await session.execute(
                    select(MaterialProperty).where(
                        MaterialProperty.material_version_id == version_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(props) == 1
        assert props[0].prop_name == "thermal_conductivity"
        assert float(props[0].value) == 0.6


@pytest.mark.asyncio
async def test_class_state_check_rejects_unknown(materials_engine: AsyncEngine) -> None:
    factory = make_session_factory(materials_engine)
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(MaterialClass(name="Weird", description="", state="archived"))


@pytest.mark.asyncio
async def test_material_state_check_rejects_unknown(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        class_id = mc.id
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_material(class_id, basics, state="deleted"))


@pytest.mark.asyncio
async def test_version_publish_state_check_rejects_unknown(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        mat = _material(mc.id, basics)
        session.add(mat)
        await session.flush()
        material_id = mat.id
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_version(material_id, basics, publish_state="live"))


@pytest.mark.asyncio
async def test_data_pedigree_check_allows_null_and_valid_rejects_unknown(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        mat = _material(mc.id, basics)
        session.add(mat)
        await session.flush()
        material_id = mat.id
        # NULL pedigree is fine
        session.add(_version(material_id, basics, ordinal=1, data_pedigree=None))
        await session.flush()
        # a valid pedigree is fine
        session.add(_version(material_id, basics, ordinal=2, data_pedigree="estimated"))
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_version(material_id, basics, ordinal=3, data_pedigree="guessed"))


@pytest.mark.asyncio
async def test_class_property_unique_per_class(materials_engine: AsyncEngine) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        class_id = mc.id
        session.add(
            MaterialClassProperty(
                class_id=class_id, slug="cp", prop_name="cp", unit="J/kg/K"
            )
        )
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                MaterialClassProperty(
                    class_id=class_id, slug="cp", prop_name="cp", unit="J/kg/K"
                )
            )


@pytest.mark.asyncio
async def test_version_ordinal_unique_per_material(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        mat = _material(mc.id, basics)
        session.add(mat)
        await session.flush()
        material_id = mat.id
        session.add(_version(material_id, basics, ordinal=1))
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_version(material_id, basics, ordinal=1))


@pytest.mark.asyncio
async def test_property_unique_per_version(materials_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        mat = _material(mc.id, basics)
        session.add(mat)
        await session.flush()
        v = _version(mat.id, basics)
        session.add(v)
        await session.flush()
        version_id = v.id
        session.add(
            MaterialProperty(
                material_version_id=version_id,
                slug="rho",
                prop_name="rho",
                value=1000,
                unit="kg/m3",
            )
        )
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                MaterialProperty(
                    material_version_id=version_id,
                    slug="rho",
                    prop_name="rho",
                    value=998,
                    unit="kg/m3",
                )
            )


@pytest.mark.asyncio
async def test_material_name_is_not_unique(materials_engine: AsyncEngine, basics: Basics) -> None:
    """Different grades/sources may share a designation — pin by id (§2.2)."""
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        session.add(_material(mc.id, basics, name="Steel"))
        session.add(_material(mc.id, basics, name="Steel"))
        await session.flush()  # no raise


@pytest.mark.asyncio
async def test_job_material_use_provenance(materials_engine: AsyncEngine, basics: Basics) -> None:
    """Composite PK (job_id, field_name): one material version per field per job."""
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        mat = _material(mc.id, basics)
        session.add(mat)
        await session.flush()
        v = _version(mat.id, basics)
        session.add(v)
        await session.flush()
        version_id = v.id
        session.add(
            JobMaterialUse(job_id=basics.job_id, field_name="fluid", material_version_id=version_id)
        )
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                JobMaterialUse(
                    job_id=basics.job_id, field_name="fluid", material_version_id=version_id
                )
            )


@pytest.mark.asyncio
async def test_retire_is_a_state_change_not_a_delete(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(materials_engine)
    async with session_scope(factory) as session:
        mc = await _new_class(session)
        mat = _material(mc.id, basics)
        session.add(mat)
        await session.flush()
        material_id = mat.id
    async with session_scope(factory) as session:
        loaded = await session.get(Material, material_id)
        assert loaded is not None
        loaded.state = "retired"
    async with session_scope(factory) as session:
        reloaded = await session.get(Material, material_id)
        assert reloaded is not None  # row persists
        assert reloaded.state == "retired"
