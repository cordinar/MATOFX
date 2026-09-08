"""Bulk import of example material classes + materials (ADR-0007 slice 9).

Operator helper, **not** auto-run by Alembic — a fresh install starts
with an empty catalog (and Alembic seeding can't reference users that
don't exist yet). Run :func:`import_example_materials` once after
install (CLI / REPL / a one-off admin script) so the materials catalog
and the in-form picker have something useful out of the box: two
classes (``Fluid``, ``Metal``) and four materials (``Water``, ``Air``,
``Steel S355``, ``Aluminium 6061-T6``) with reference values from
NIST / EN / MMPDS / Engineering Toolbox.

Idempotent — classes existing by name are skipped (their schema is
left alone), and a material whose name already exists in its class is
skipped too, so re-running the seed is safe.

The actor must hold ``MATERIALS_CURATE`` (super-users do, by default).
The publish-approval gate is **bypassed** for the seed
(``approval_required=False``); seeding is an operator action by
design, not a user submission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.materials import PropertySpec, make_materials_service
from matofx.materials.orm import Material, MaterialClass

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter

# Repo-root ``example_materials/seed.json`` — the shipped reference set.
DEFAULT_SEED_PATH: Path = Path(__file__).resolve().parents[3] / "example_materials" / "seed.json"


@dataclass(frozen=True)
class SeedResult:
    """Summary of one :func:`import_example_materials` run."""

    classes_created: list[str] = field(default_factory=list)
    classes_skipped: list[str] = field(default_factory=list)
    materials_created: list[str] = field(default_factory=list)
    materials_skipped: list[str] = field(default_factory=list)


async def import_example_materials(
    session: AsyncSession,
    *,
    actor_user_id: int,
    audit: AuditWriter,
    source: Path | None = None,
) -> SeedResult:
    """Load classes + materials from ``source`` (default: shipped seed).

    Args:
        session: Open async DB session (caller commits).
        actor_user_id: Curator running the seed; must hold
            ``MATERIALS_CURATE`` (super-users do).
        audit: The shared audit writer; every create is audited.
        source: Path to a seed JSON file with ``classes`` + ``materials``
            arrays; defaults to :data:`DEFAULT_SEED_PATH`.

    Returns:
        :class:`SeedResult` listing what was created vs skipped.
    """
    path = source or DEFAULT_SEED_PATH
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    service = make_materials_service(audit)
    result = SeedResult()

    classes_by_name: dict[str, int] = {}
    for spec in data.get("classes") or []:
        name = str(spec["name"])
        existing = await _find_class_by_name(session, name)
        if existing is not None:
            classes_by_name[name] = int(existing.id)
            result.classes_skipped.append(name)
            continue
        cls = await service.create_class(
            session,
            actor_user_id=actor_user_id,
            name=name,
            description=str(spec.get("description") or ""),
        )
        for prop in spec.get("properties") or []:
            await service.add_class_property(
                session,
                actor_user_id=actor_user_id,
                class_id=cls.id,
                prop_name=str(prop["prop_name"]),
                unit=str(prop["unit"]),
                required=bool(prop.get("required", True)),
            )
        classes_by_name[name] = int(cls.id)
        result.classes_created.append(name)

    for spec in data.get("materials") or []:
        name = str(spec["name"])
        class_name = str(spec["class"])
        class_id = classes_by_name.get(class_name)
        if class_id is None:
            # Class missing from the seed — refuse silently rather than
            # half-creating; the operator's seed JSON is the contract.
            result.materials_skipped.append(f"{name} (class {class_name!r} not seeded)")
            continue
        if await _material_exists(session, name=name, class_id=class_id):
            result.materials_skipped.append(name)
            continue
        properties = [
            PropertySpec(
                prop_name=str(p["prop_name"]),
                value=float(p["value"]),
                unit=str(p["unit"]),
            )
            for p in spec.get("properties") or []
        ]
        await service.create_material(
            session,
            actor_user_id=actor_user_id,
            name=name,
            class_id=class_id,
            properties=properties,
            provenance=str(spec.get("provenance") or "seed"),
            description=str(spec.get("description") or ""),
            aliases=list(spec.get("aliases") or []),
            tags=list(spec.get("tags") or []),
            reference=spec.get("reference"),
            approval_required=False,  # operator action — bypass the gate
        )
        result.materials_created.append(name)

    return result


async def _find_class_by_name(session: AsyncSession, name: str) -> MaterialClass | None:
    return (
        await session.execute(select(MaterialClass).where(MaterialClass.name == name))
    ).scalar_one_or_none()


async def _material_exists(session: AsyncSession, *, name: str, class_id: int) -> bool:
    stmt = select(Material.id).where(Material.name == name, Material.class_id == class_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None
