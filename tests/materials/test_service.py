"""Service tests for the materials-database lifecycle (slice 2).

Covers the design §11 plan that lives at the service layer: class-schema
validation (required props + unit match), immutable versioning,
resolve-and-freeze reproducibility, the curator-permission gate,
retire/restore state-only semantics, and optimistic locking. The
``basics`` fixture seeds a curator (super_user) + a job; this module adds
a regular (non-curator) user to exercise the gate.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from matofx.approvals import ApprovalRequest
from matofx.approvals.errors import SelfApprovalNotAllowedError
from matofx.audit import AuditWriter
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.materials import (
    ApproverRequiredError,
    DuplicateClassPropertyError,
    DuplicateMaterialClassError,
    DuplicateMaterialPropertyError,
    Material,
    MaterialClassPropertyNotFoundError,
    MaterialClassRetiredError,
    MaterialConcurrentModificationError,
    MaterialRetiredError,
    MaterialSchemaError,
    MaterialsService,
    MaterialValidationError,
    PropertySpec,
    make_materials_service,
)
from matofx.materials.orm import (
    JobMaterialUse,
    MaterialClassProperty,
    MaterialProperty,
    MaterialVersion,
)
from matofx.users import make_password_hasher, make_user_service
from matofx.users.errors import PermissionDeniedError
from matofx.users.orm import User
from matofx.users.permissions import Permission
from matofx.users.role_orm import RolePermission

from .conftest import Basics


@dataclass
class Mat:
    """Service + the ids a materials test needs."""

    svc: MaterialsService
    factory: async_sessionmaker[AsyncSession]
    curator: int  # "mei" — a super_user (so a materials curator)
    curator2: int  # "zoe" — a second curator, the directed approver in gate tests
    regular: int  # "obs" — a regular (non-curator) user
    job_id: int


_CURATOR2_NAME = "zoe"


@pytest.fixture
async def mat(materials_engine: AsyncEngine, basics: Basics) -> Mat:
    """A wired service, the seeded curator, a second curator, and a regular user."""
    factory = make_session_factory(materials_engine)
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    user_service = make_user_service(
        auth, AuditWriter(signing_key=None, key_id=None), hasher=make_password_hasher(auth)
    )
    async with session_scope(factory) as session:
        obs = await user_service.create_user(
            session,
            username="obs",
            email="obs@example.com",
            password="obspassword",
            full_name="Obs",
            role="regular_user",
        )
        zoe = await user_service.create_user(
            session,
            username=_CURATOR2_NAME,
            email="zoe@example.com",
            password="zoepassword",
            full_name="Zoe",
            role="super_user",
        )
        obs_id, zoe_id = obs.id, zoe.id
    svc = make_materials_service(AuditWriter(signing_key=None, key_id=None))
    return Mat(
        svc=svc,
        factory=factory,
        curator=basics.user_id,
        curator2=zoe_id,
        regular=obs_id,
        job_id=basics.job_id,
    )


async def _fluid_class(mat: Mat) -> int:
    """A 'Fluid' class requiring thermal_conductivity, with optional density."""
    async with session_scope(mat.factory) as session:
        cls = await mat.svc.create_class(
            session, actor_user_id=mat.curator, name="Fluid", description="liquids + gases"
        )
        await mat.svc.add_class_property(
            session,
            actor_user_id=mat.curator,
            class_id=cls.id,
            prop_name="thermal_conductivity",
            unit="W/m/K",
            required=True,
        )
        await mat.svc.add_class_property(
            session,
            actor_user_id=mat.curator,
            class_id=cls.id,
            prop_name="density",
            unit="kg/m3",
            required=False,
        )
        return cls.id


async def _water(mat: Mat, class_id: int, **overrides: object) -> int:
    """Create a published 'Water' material satisfying the Fluid schema; return its id.

    Passes ``approval_required=False`` so the helper publishes directly —
    the approval gate has its own dedicated tests.
    """
    kwargs: dict[str, object] = {
        "name": "Water",
        "class_id": class_id,
        "provenance": "handbook",
        "approval_required": False,
        "properties": [
            PropertySpec("thermal_conductivity", 0.6, "W/m/K"),
            PropertySpec("density", 998.0, "kg/m3"),
        ],
    }
    kwargs.update(overrides)
    async with session_scope(mat.factory) as session:
        material = await mat.svc.create_material(session, actor_user_id=mat.curator, **kwargs)  # type: ignore[arg-type]
        return material.id


# --------------------------------------------------------------------------
# class schema + create
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_material_publishes_first_version_with_properties(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        versions = await mat.svc.list_versions(session, material_id)
        assert len(versions) == 1
        assert versions[0].ordinal == 1
        assert versions[0].publish_state == "published"  # curator direct-publish (slice 2)
        props = {
            p.prop_name: (float(p.value), p.unit)
            for p in await mat.svc.version_properties(session, versions[0].id)
        }
        assert props == {
            "thermal_conductivity": (0.6, "W/m/K"),
            "density": (998.0, "kg/m3"),
        }


@pytest.mark.asyncio
async def test_create_material_requires_required_property(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialSchemaError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Air",
                class_id=class_id,
                provenance="handbook",
                properties=[PropertySpec("density", 1.2, "kg/m3")],  # missing thermal_conductivity
            )


@pytest.mark.asyncio
async def test_create_material_enforces_required_unit(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialSchemaError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Oil",
                class_id=class_id,
                provenance="handbook",
                properties=[
                    PropertySpec("thermal_conductivity", 0.15, "BTU/hr/ft/F")
                ],  # wrong unit
            )


@pytest.mark.asyncio
async def test_extra_property_beyond_schema_is_allowed(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        material = await mat.svc.create_material(
            session,
            actor_user_id=mat.curator,
            name="Glycol",
            class_id=class_id,
            provenance="handbook",
            approval_required=False,
            properties=[
                PropertySpec("thermal_conductivity", 0.25, "W/m/K"),
                PropertySpec("viscosity", 0.016, "Pa.s"),  # not in the schema
            ],
        )
        assert material.id > 0


@pytest.mark.asyncio
async def test_duplicate_material_property_rejected(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(DuplicateMaterialPropertyError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Dup",
                class_id=class_id,
                provenance="handbook",
                properties=[
                    PropertySpec("thermal_conductivity", 0.6, "W/m/K"),
                    PropertySpec("thermal_conductivity", 0.7, "W/m/K"),
                ],
            )


@pytest.mark.asyncio
async def test_invalid_data_pedigree_rejected(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialValidationError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Water",
                class_id=class_id,
                provenance="handbook",
                data_pedigree="guessed",
                properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
            )


@pytest.mark.asyncio
async def test_duplicate_class_and_property_names_rejected(mat: Mat) -> None:
    async with session_scope(mat.factory) as session:
        cls = await mat.svc.create_class(session, actor_user_id=mat.curator, name="Metal")
        class_id = cls.id
    async with session_scope(mat.factory) as session:
        with pytest.raises(DuplicateMaterialClassError):
            await mat.svc.create_class(session, actor_user_id=mat.curator, name="Metal")
    async with session_scope(mat.factory) as session:
        await mat.svc.add_class_property(
            session, actor_user_id=mat.curator, class_id=class_id, prop_name="E", unit="GPa"
        )
    async with session_scope(mat.factory) as session:
        with pytest.raises(DuplicateClassPropertyError):
            await mat.svc.add_class_property(
                session, actor_user_id=mat.curator, class_id=class_id, prop_name="E", unit="GPa"
            )


@pytest.mark.asyncio
async def test_create_under_retired_class_rejected(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        await mat.svc.retire_class(session, actor_user_id=mat.curator, class_id=class_id)
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialClassRetiredError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Water",
                class_id=class_id,
                provenance="handbook",
                properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
            )


# --------------------------------------------------------------------------
# immutable versioning + reproducible resolve
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_version_is_append_only(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        v2 = await mat.svc.add_version(
            session,
            actor_user_id=mat.curator,
            material_id=material_id,
            provenance="re-measured",
            approval_required=False,
            properties=[PropertySpec("thermal_conductivity", 0.62, "W/m/K")],
        )
        assert v2.ordinal == 2
    async with session_scope(mat.factory) as session:
        versions = await mat.svc.list_versions(session, material_id)
        assert [v.ordinal for v in versions] == [2, 1]


@pytest.mark.asyncio
async def test_resolve_freezes_and_a_later_edit_does_not_change_it(mat: Mat) -> None:
    """A finished job's frozen block is unaffected by a later version (reproducibility)."""
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        v1 = (await mat.svc.list_versions(session, material_id))[0]
        v1_id = v1.id
        block = await mat.svc.resolve_for_submit(session, material_version_id=v1_id)
        assert block["name"] == "Water"
        assert block["class"] == "Fluid"
        # Properties are keyed by slug; each value carries the display
        # ``prop_name`` alongside value + unit (ADR-0007 §6).
        assert block["properties"]["thermal_conductivity"] == {
            "prop_name": "thermal_conductivity",
            "value": 0.6,
            "unit": "W/m/K",
        }
    # add a new version with a different value
    async with session_scope(mat.factory) as session:
        await mat.svc.add_version(
            session,
            actor_user_id=mat.curator,
            material_id=material_id,
            provenance="re-measured",
            approval_required=False,
            properties=[PropertySpec("thermal_conductivity", 0.99, "W/m/K")],
        )
    # resolving v1 still yields the original frozen value
    async with session_scope(mat.factory) as session:
        block = await mat.svc.resolve_for_submit(session, material_version_id=v1_id)
        assert block["properties"]["thermal_conductivity"]["value"] == 0.6


@pytest.mark.asyncio
async def test_resolve_missing_required_property_raises(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        version_id = (await mat.svc.list_versions(session, material_id))[0].id
        # an explicit required-property list the version doesn't satisfy
        with pytest.raises(MaterialSchemaError):
            await mat.svc.resolve_for_submit(
                session,
                material_version_id=version_id,
                required_properties=["thermal_conductivity", "specific_heat"],
            )


# --------------------------------------------------------------------------
# permission gate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_class_management_is_curate_only_but_contributing_is_base_tier(
    mat: Mat,
) -> None:
    """The two halves of the materials gate, which are not the same.

    Class management stayed **curate-only**; *contributing* a material
    was reclassified to base tier by migration 0083 (``materials.create``
    seeded onto every base role, alongside ``cohorts.create`` and
    friends), so a regular user may add one.

    This test previously asserted that a regular user was refused *both*.
    The class half still holds and is kept. The material half now checks
    the real rule: allowed, and then refused once the grant is stripped
    — because "everyone holds it by default" and "nothing checks it"
    are indistinguishable if you only ever test the allowed path.
    """
    async with session_scope(mat.factory) as session:
        with pytest.raises(PermissionDeniedError):
            await mat.svc.create_class(session, actor_user_id=mat.regular, name="Sneaky")

    class_id = await _fluid_class(mat)

    # Contributing is base-tier: this is allowed. The approval gate is a
    # separate concern and is exercised elsewhere, so publish directly.
    async with session_scope(mat.factory) as session:
        material = await mat.svc.create_material(
            session,
            actor_user_id=mat.regular,
            name="Water",
            class_id=class_id,
            provenance="handbook",
            properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
            approval_required=False,
        )
        assert material.id is not None

    async with session_scope(mat.factory) as session:
        await _revoke_permission(session, mat.regular, Permission.MATERIALS_CREATE)

    async with session_scope(mat.factory) as session:
        with pytest.raises(PermissionDeniedError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.regular,
                name="Heavy water",
                class_id=class_id,
                provenance="handbook",
                properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
                approval_required=False,
            )


@pytest.mark.asyncio
async def _revoke_permission(session, user_id: int, permission: Permission) -> None:
    """Drop ``permission`` from the user's base role, to prove the gate bites."""
    role_id = (await session.execute(select(User.role_id).where(User.id == user_id))).scalar_one()
    await session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission == permission.value,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_non_curator_can_read(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        # reads are open to any authenticated user (no actor gate)
        material = await mat.svc.get(session, material_id)
        assert material.name == "Water"
        results = await mat.svc.search(session, name="wat")
        assert [m.id for m in results] == [material_id]


# --------------------------------------------------------------------------
# retire / restore + optimistic lock
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_blocks_mutation_and_restore_reverses(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        await mat.svc.retire(session, actor_user_id=mat.curator, material_id=material_id)
    async with session_scope(mat.factory) as session:
        row = await session.get(Material, material_id)
        assert row is not None  # state-only, no hard delete
        assert row.state == "retired"
        with pytest.raises(MaterialRetiredError):
            await mat.svc.add_version(
                session,
                actor_user_id=mat.curator,
                material_id=material_id,
                provenance="x",
                properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
            )
    async with session_scope(mat.factory) as session:
        restored = await mat.svc.restore(
            session, actor_user_id=mat.curator, material_id=material_id
        )
        assert restored.state == "active"


@pytest.mark.asyncio
async def test_update_metadata_optimistic_lock(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        updated = await mat.svc.update_material_metadata(
            session,
            actor_user_id=mat.curator,
            material_id=material_id,
            expected_version=1,
            tags=["coolant"],
        )
        assert updated.tags == ["coolant"]
        assert updated.version == 2
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialConcurrentModificationError):
            await mat.svc.update_material_metadata(
                session,
                actor_user_id=mat.curator,
                material_id=material_id,
                expected_version=1,
                tags=["stale"],
            )


# --------------------------------------------------------------------------
# search + provenance
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_class_and_tag(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    await _water(mat, class_id, name="Water", tags=["coolant"])
    await _water(mat, class_id, name="Brine", tags=["corrosive"])
    async with session_scope(mat.factory) as session:
        by_class = await mat.svc.search(session, class_id=class_id)
        assert {m.name for m in by_class} == {"Water", "Brine"}
        by_tag = await mat.svc.search(session, tag="coolant")
        assert [m.name for m in by_tag] == ["Water"]


@pytest.mark.asyncio
async def test_record_job_use_writes_provenance(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    async with session_scope(mat.factory) as session:
        version_id = (await mat.svc.list_versions(session, material_id))[0].id
    async with session_scope(mat.factory) as session:
        await mat.svc.record_job_use(
            session, job_id=mat.job_id, field_name="fluid", material_version_id=version_id
        )
    async with session_scope(mat.factory) as session:
        row = await session.get(JobMaterialUse, (mat.job_id, "fluid"))
        assert row is not None
        assert row.material_version_id == version_id


# --------------------------------------------------------------------------
# directed-approver gate (ADR-0007 §5)
# --------------------------------------------------------------------------


async def _create_pending(mat: Mat, class_id: int) -> tuple[int, int, int]:
    """mei proposes a gated material with zoe as approver. Returns ids."""
    async with session_scope(mat.factory) as session:
        material = await mat.svc.create_material(
            session,
            actor_user_id=mat.curator,
            name="Water",
            class_id=class_id,
            provenance="handbook",
            properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
            approval_required=True,
            designated_approver_user_id=mat.curator2,
            designated_approver_username=_CURATOR2_NAME,
        )
        material_id = material.id
        assert material.state == "pending"
        version = (
            await session.execute(
                select(MaterialVersion).where(MaterialVersion.material_id == material_id)
            )
        ).scalar_one()
        assert version.publish_state == "pending"
        request = (
            await session.execute(
                select(ApprovalRequest).where(ApprovalRequest.material_version_id == version.id)
            )
        ).scalar_one()
        assert request.designated_approver_user_id == mat.curator2
        return material_id, version.id, request.id


@pytest.mark.asyncio
async def test_gated_create_lands_pending_with_request(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    await _create_pending(mat, class_id)


@pytest.mark.asyncio
async def test_gated_create_requires_an_approver(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(ApproverRequiredError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Water",
                class_id=class_id,
                provenance="handbook",
                properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
                approval_required=True,
            )


@pytest.mark.asyncio
async def test_gated_create_rejects_self_approval(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(SelfApprovalNotAllowedError):
            await mat.svc.create_material(
                session,
                actor_user_id=mat.curator,
                name="Water",
                class_id=class_id,
                provenance="handbook",
                properties=[PropertySpec("thermal_conductivity", 0.6, "W/m/K")],
                approval_required=True,
                designated_approver_user_id=mat.curator,
                designated_approver_username="mei",
            )


@pytest.mark.asyncio
async def test_gated_approve_publishes(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id, version_id, request_id = await _create_pending(mat, class_id)
    async with session_scope(mat.factory) as session:
        request, version = await mat.svc.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=mat.curator2,
            decider_username=_CURATOR2_NAME,
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "approved"
        assert version.publish_state == "published"
    async with session_scope(mat.factory) as session:
        row = await session.get(Material, material_id)
        assert row is not None
        assert row.state == "active"
        v = await session.get(MaterialVersion, version_id)
        assert v is not None
        assert v.publish_state == "published"


@pytest.mark.asyncio
async def test_gated_reject_marks_version_rejected(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    material_id, version_id, request_id = await _create_pending(mat, class_id)
    async with session_scope(mat.factory) as session:
        request, version = await mat.svc.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=mat.curator2,
            decider_username=_CURATOR2_NAME,
            decision="deny",
            reason="needs a literature source",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "denied"
        assert version.publish_state == "rejected"
    async with session_scope(mat.factory) as session:
        # the material + version rows persist (no hard delete)
        assert await session.get(Material, material_id) is not None
        assert await session.get(MaterialVersion, version_id) is not None


# --------------------------------------------------------------------------
# class-property edit / delete (forward-only schema changes)
# --------------------------------------------------------------------------


async def _first_prop_id(mat: Mat, class_id: int, name: str) -> int:
    async with session_scope(mat.factory) as session:
        schema = await mat.svc.class_schema(session, class_id)
        return next(p.id for p in schema if p.prop_name == name)


@pytest.mark.asyncio
async def test_update_class_property_renames_schema_but_keeps_version_snapshots(
    mat: Mat,
) -> None:
    """Renaming a class property is forward-only: past MaterialProperty rows untouched."""
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    pid = await _first_prop_id(mat, class_id, "thermal_conductivity")

    async with session_scope(mat.factory) as session:
        await mat.svc.update_class_property(
            session,
            actor_user_id=mat.curator,
            class_id=class_id,
            property_id=pid,
            prop_name="k_thermal",
            unit="W/(m·K)",
        )

    async with session_scope(mat.factory) as session:
        # Schema updated.
        prop = await session.get(MaterialClassProperty, pid)
        assert prop is not None
        assert prop.prop_name == "k_thermal"
        assert prop.unit == "W/(m·K)"
        # Past version still has the original snapshot — immutable.
        versions = await mat.svc.list_versions(session, material_id)
        snap = {
            p.prop_name: p.unit for p in await mat.svc.version_properties(session, versions[0].id)
        }
        assert "thermal_conductivity" in snap
        assert snap["thermal_conductivity"] == "W/m/K"
        assert "k_thermal" not in snap


@pytest.mark.asyncio
async def test_update_class_property_required_flag_only(mat: Mat) -> None:
    """Pass only ``required`` → name and unit are unchanged."""
    class_id = await _fluid_class(mat)
    pid = await _first_prop_id(mat, class_id, "density")
    async with session_scope(mat.factory) as session:
        await mat.svc.update_class_property(
            session,
            actor_user_id=mat.curator,
            class_id=class_id,
            property_id=pid,
            required=True,
        )
        prop = await session.get(MaterialClassProperty, pid)
        assert prop is not None
        assert prop.prop_name == "density"
        assert prop.unit == "kg/m3"
        assert prop.required is True


@pytest.mark.asyncio
async def test_update_class_property_rejects_rename_to_existing_name(mat: Mat) -> None:
    """Renaming to a name already used in the class hits the UNIQUE constraint."""
    class_id = await _fluid_class(mat)
    pid = await _first_prop_id(mat, class_id, "density")
    async with session_scope(mat.factory) as session:
        with pytest.raises(DuplicateClassPropertyError):
            await mat.svc.update_class_property(
                session,
                actor_user_id=mat.curator,
                class_id=class_id,
                property_id=pid,
                prop_name="thermal_conductivity",
            )


@pytest.mark.asyncio
async def test_update_class_property_validates_empty(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    pid = await _first_prop_id(mat, class_id, "thermal_conductivity")
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialValidationError):
            await mat.svc.update_class_property(
                session,
                actor_user_id=mat.curator,
                class_id=class_id,
                property_id=pid,
                prop_name="   ",
            )
        with pytest.raises(MaterialValidationError):
            await mat.svc.update_class_property(
                session,
                actor_user_id=mat.curator,
                class_id=class_id,
                property_id=pid,
                unit="",
            )


@pytest.mark.asyncio
async def test_update_class_property_requires_curator(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    pid = await _first_prop_id(mat, class_id, "thermal_conductivity")
    async with session_scope(mat.factory) as session:
        with pytest.raises(PermissionDeniedError):
            await mat.svc.update_class_property(
                session,
                actor_user_id=mat.regular,
                class_id=class_id,
                property_id=pid,
                required=False,
            )


@pytest.mark.asyncio
async def test_update_class_property_404_when_not_on_class(mat: Mat) -> None:
    """Editing a property id that belongs to a *different* class refuses."""
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        other = await mat.svc.create_class(
            session, actor_user_id=mat.curator, name="Metal", description=""
        )
        other_id = other.id
        await mat.svc.add_class_property(
            session,
            actor_user_id=mat.curator,
            class_id=other_id,
            prop_name="density",
            unit="kg/m3",
        )
    other_pid = await _first_prop_id(mat, other_id, "density")
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialClassPropertyNotFoundError):
            await mat.svc.update_class_property(
                session,
                actor_user_id=mat.curator,
                class_id=class_id,  # wrong class for other_pid
                property_id=other_pid,
                required=True,
            )


@pytest.mark.asyncio
async def test_remove_class_property_drops_schema_row_but_keeps_version_snapshots(
    mat: Mat,
) -> None:
    """Deletion is forward-only: past MaterialProperty rows on existing versions stay."""
    class_id = await _fluid_class(mat)
    material_id = await _water(mat, class_id)
    pid = await _first_prop_id(mat, class_id, "density")

    async with session_scope(mat.factory) as session:
        await mat.svc.remove_class_property(
            session, actor_user_id=mat.curator, class_id=class_id, property_id=pid
        )

    async with session_scope(mat.factory) as session:
        # Schema row gone.
        assert await session.get(MaterialClassProperty, pid) is None
        # Past version still has the density snapshot — untouched.
        versions = await mat.svc.list_versions(session, material_id)
        snap_names = {
            p.prop_name for p in await mat.svc.version_properties(session, versions[0].id)
        }
        assert "density" in snap_names


@pytest.mark.asyncio
async def test_remove_class_property_requires_curator(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    pid = await _first_prop_id(mat, class_id, "density")
    async with session_scope(mat.factory) as session:
        with pytest.raises(PermissionDeniedError):
            await mat.svc.remove_class_property(
                session, actor_user_id=mat.regular, class_id=class_id, property_id=pid
            )


@pytest.mark.asyncio
async def test_remove_class_property_404_when_not_on_class(mat: Mat) -> None:
    class_id = await _fluid_class(mat)
    async with session_scope(mat.factory) as session:
        with pytest.raises(MaterialClassPropertyNotFoundError):
            await mat.svc.remove_class_property(
                session, actor_user_id=mat.curator, class_id=class_id, property_id=99999
            )
        # Silences "MaterialProperty imported but unused" — used implicitly by snapshots.
        _ = MaterialProperty
