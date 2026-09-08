"""Example-materials seed (ADR-0007 slice 9).

The seed is an operator helper, not an Alembic migration. These tests
pin down the end-to-end behaviour: the shipped JSON seeds the expected
classes + materials, the seed is idempotent on re-run, and a missing
class in the JSON skips its materials rather than half-creating.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.materials.orm import Material, MaterialClass, MaterialProperty
from matofx.materials.seed import (
    DEFAULT_SEED_PATH,
    SeedResult,
    import_example_materials,
)

from .conftest import Basics


@pytest.mark.asyncio
async def test_shipped_seed_creates_expected_classes_and_materials(
    materials_engine: AsyncEngine, basics: Basics
) -> None:
    """The shipped ``example_materials/seed.json`` loads end-to-end."""
    factory = make_session_factory(materials_engine)
    audit = AuditWriter(signing_key=None, key_id=None)

    async with session_scope(factory) as session:
        result = await import_example_materials(session, actor_user_id=basics.user_id, audit=audit)
    assert isinstance(result, SeedResult)
    assert set(result.classes_created) == {"Fluid", "Metal"}
    assert {"Water", "Air", "Steel S355", "Aluminium 6061-T6"} <= set(result.materials_created)
    assert not result.classes_skipped
    assert not result.materials_skipped

    # Class schema landed (Fluid requires thermal_conductivity + density).
    async with session_scope(factory) as session:
        fluid = (
            await session.execute(select(MaterialClass).where(MaterialClass.name == "Fluid"))
        ).scalar_one()
        water = (
            await session.execute(select(Material).where(Material.name == "Water"))
        ).scalar_one()
        assert water.class_id == fluid.id
        # The material's first version has Water's properties.
        from matofx.materials.orm import MaterialVersion  # noqa: PLC0415

        v = (
            await session.execute(
                select(MaterialVersion).where(MaterialVersion.material_id == water.id)
            )
        ).scalar_one()
        props = (
            (
                await session.execute(
                    select(MaterialProperty).where(MaterialProperty.material_version_id == v.id)
                )
            )
            .scalars()
            .all()
        )
        by_name = {p.prop_name: float(p.value) for p in props}
        assert by_name["thermal_conductivity"] == pytest.approx(0.598)
        assert by_name["density"] == pytest.approx(998.2)
        # The seed bypasses the gate → direct-publish, version is published.
        assert v.publish_state == "published"


@pytest.mark.asyncio
async def test_seed_is_idempotent_on_re_run(materials_engine: AsyncEngine, basics: Basics) -> None:
    """Re-running the seed skips existing classes + materials (safe to repeat)."""
    factory = make_session_factory(materials_engine)
    audit = AuditWriter(signing_key=None, key_id=None)

    async with session_scope(factory) as session:
        await import_example_materials(session, actor_user_id=basics.user_id, audit=audit)
    async with session_scope(factory) as session:
        second = await import_example_materials(session, actor_user_id=basics.user_id, audit=audit)
    assert not second.classes_created
    assert not second.materials_created
    assert set(second.classes_skipped) == {"Fluid", "Metal"}
    assert "Water" in second.materials_skipped


@pytest.mark.asyncio
async def test_seed_skips_material_when_class_missing_from_json(
    materials_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    """A material whose declared class isn't in the seed JSON is skipped, not crashed."""
    factory = make_session_factory(materials_engine)
    audit = AuditWriter(signing_key=None, key_id=None)
    custom = tmp_path / "tiny_seed.json"
    custom.write_text(
        json.dumps(
            {
                "classes": [{"name": "Fluid", "properties": []}],
                "materials": [
                    {
                        "name": "Water",
                        "class": "Fluid",
                        "provenance": "test",
                        "properties": [],
                    },
                    {
                        "name": "OrphanMetal",
                        "class": "Metal",
                        "provenance": "test",
                        "properties": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    async with session_scope(factory) as session:
        result = await import_example_materials(
            session, actor_user_id=basics.user_id, audit=audit, source=custom
        )
    assert result.materials_created == ["Water"]
    assert any("OrphanMetal" in s for s in result.materials_skipped)


def test_default_seed_path_resolves_to_shipped_json() -> None:
    """``DEFAULT_SEED_PATH`` points at the shipped seed file."""
    assert DEFAULT_SEED_PATH.is_file(), f"missing: {DEFAULT_SEED_PATH}"
    data = json.loads(DEFAULT_SEED_PATH.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["classes"]} == {"Fluid", "Metal"}
