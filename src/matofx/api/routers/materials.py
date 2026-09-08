"""HTTP API for the materials database (ADR-0007 §6).

Thin endpoints that delegate to :class:`~matofx.materials.MaterialsService`;
domain errors propagate to the central handlers in
:mod:`matofx.api.errors` (the ``MaterialsError`` family + the
``ApprovalError`` family raised by the directed-approver gate), so there
is no per-endpoint try/except.

Gating (design §6 / project-wide toggle rule):

- **Feature flag** — a router-wide dependency 404s every route when
  ``materials_database`` is off, so the surface is *absent* for a
  customer without the flag.
- **Permission** — reads are open to any authenticated user (materials
  are global); create / edit / retire require ``MATERIALS_CURATE``,
  enforced by the service (``PermissionDeniedError`` → 403).
- **Approval** — create / add-version resolve the
  ``materials_change_approval`` toggle here and pass it to the service
  with the author's nominated approver; with the gate on, the change
  lands ``pending`` until a *second* curator approves it.

Approve / reject reuse the approvals surface (the directed-approver
extension) — they are not materials-router endpoints.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._approval_notify import notify_pending_approval
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_materials_service,
    get_notifier,
    get_settings,
)
from matofx.api.schemas import (
    MaterialAddVersionRequest,
    MaterialApproverOption,
    MaterialClassCreateRequest,
    MaterialClassPropertyCreateRequest,
    MaterialClassPropertyPatch,
    MaterialClassPropertyResponse,
    MaterialClassResponse,
    MaterialCreateRequest,
    MaterialDetailResponse,
    MaterialMetadataPatch,
    MaterialPickerCandidate,
    MaterialPropertyPayload,
    MaterialPropertyResponse,
    MaterialResponse,
    MaterialVersionResponse,
)
from matofx.approvals.orm import ApprovalRequest
from matofx.config import Settings
from matofx.config.general_service import load_general_settings
from matofx.features import Feature, feature_enabled
from matofx.materials import (
    MaterialPickerEntry,
    MaterialsService,
    PropertySpec,
)
from matofx.materials.orm import Material, MaterialClass, MaterialProperty, MaterialVersion
from matofx.notify import Notifier
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission


def _require_materials_feature(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 every route when the materials feature flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.MATERIALS_DATABASE):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_materials_feature)])

_Db = Annotated[AsyncSession, Depends(get_db)]
_Me = Annotated[User, Depends(get_current_user)]
_Svc = Annotated[MaterialsService, Depends(get_materials_service)]
_Notify = Annotated[Notifier, Depends(get_notifier)]


async def _notify_material_pending(db: AsyncSession, notifier: Notifier, material_id: int) -> None:
    """Email the chosen materials curator if the newest version is gated.

    No-op when nothing is pending.
    """
    version_id = (
        await db.execute(
            select(MaterialVersion.id)
            .where(MaterialVersion.material_id == material_id)
            .order_by(MaterialVersion.ordinal.desc())
        )
    ).scalars().first()
    if version_id is not None:
        await notify_pending_approval(
            db,
            notifier,
            target_column=ApprovalRequest.material_version_id,
            target_value=version_id,
        )


# ---- helpers -------------------------------------------------------------


def _specs(payload: list[MaterialPropertyPayload]) -> list[PropertySpec]:
    return [
        PropertySpec(
            prop_name=p.prop_name,
            value=p.value,
            unit=p.unit,
            slug=p.slug,
            source=p.source,
            uncertainty=p.uncertainty,
            notes=p.notes,
        )
        for p in payload
    ]


async def _approval_required(session: AsyncSession) -> bool:
    """Resolve the ``materials_change_approval`` toggle (default ON)."""
    values = await load_general_settings(session)
    return bool(values.get("materials_change_approval", True))


async def _resolve_approver(
    session: AsyncSession, approver_user_id: int | None
) -> tuple[int | None, str | None]:
    """Validate the nominated approver is an active curator; return (id, username)."""
    if approver_user_id is None:
        return None, None
    user = await session.get(User, approver_user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The chosen reviewer isn't an active user.",
        )
    if not await user_has_permission(session, user.id, Permission.MATERIALS_CURATE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The chosen reviewer isn't a materials curator.",
        )
    return user.id, user.username


def _property_response(p: MaterialProperty) -> MaterialPropertyResponse:
    return MaterialPropertyResponse(
        slug=p.slug,
        prop_name=p.prop_name,
        value=float(p.value),
        unit=p.unit,
        source=p.source,
        uncertainty=p.uncertainty,
        notes=p.notes,
    )


async def _material_response(
    session: AsyncSession, material: Material, *, version_count: int = 0
) -> MaterialResponse:
    cls = await session.get(MaterialClass, material.class_id)
    return MaterialResponse(
        id=material.id,
        name=material.name,
        aliases=list(material.aliases or []),
        class_id=material.class_id,
        class_name=cls.name if cls is not None else "",
        description=material.description,
        tags=list(material.tags or []),
        steward_user_id=material.steward_user_id,
        confidentiality=material.confidentiality,
        state=material.state,
        version=material.version,
        created_at=material.created_at,
        version_count=version_count,
    )


async def _fetch_material_version_counts(
    session: AsyncSession, material_ids: list[int]
) -> dict[int, int]:
    """One aggregate query → ``{material_id: count}`` (avoids N+1 on list)."""
    if not material_ids:
        return {}
    stmt = (
        select(MaterialVersion.material_id, func.count())
        .where(MaterialVersion.material_id.in_(material_ids))
        .group_by(MaterialVersion.material_id)
    )
    return {int(mid): int(n) for mid, n in (await session.execute(stmt)).all()}


async def _version_response(
    svc: MaterialsService, session: AsyncSession, version: MaterialVersion
) -> MaterialVersionResponse:
    props = await svc.version_properties(session, version.id)
    return MaterialVersionResponse(
        id=version.id,
        ordinal=version.ordinal,
        provenance=version.provenance,
        reference=version.reference,
        data_pedigree=version.data_pedigree,
        conditions=version.conditions,
        version_notes=version.version_notes,
        effective_date=version.effective_date,
        publish_state=version.publish_state,
        created_at=version.created_at,
        properties=[_property_response(p) for p in props],
    )


async def _class_response(
    svc: MaterialsService, session: AsyncSession, cls: MaterialClass
) -> MaterialClassResponse:
    schema = await svc.class_schema(session, cls.id)
    return MaterialClassResponse(
        id=cls.id,
        name=cls.name,
        description=cls.description,
        state=cls.state,
        properties=[
            MaterialClassPropertyResponse(
                id=cp.id,
                slug=cp.slug,
                prop_name=cp.prop_name,
                unit=cp.unit,
                required=cp.required,
            )
            for cp in schema
        ],
    )


async def _detail_response(
    svc: MaterialsService, session: AsyncSession, material_id: int
) -> MaterialDetailResponse:
    material = await svc.get(session, material_id)
    versions = await svc.list_versions(session, material_id)
    return MaterialDetailResponse(
        material=await _material_response(session, material, version_count=len(versions)),
        versions=[await _version_response(svc, session, v) for v in versions],
    )


def _picker_candidate(entry: MaterialPickerEntry) -> MaterialPickerCandidate:
    # ``entry.properties`` is slug-keyed; each value carries the display
    # ``prop_name`` alongside value / unit so the UI can render labels.
    return MaterialPickerCandidate(
        material_id=entry.material_id,
        name=entry.name,
        class_name=entry.class_name,
        version_id=entry.version_id,
        version_ordinal=entry.version_ordinal,
        properties={
            slug: MaterialPropertyResponse(
                slug=slug,
                prop_name=str(d.get("prop_name") or slug),
                value=float(d["value"]),
                unit=str(d["unit"]),
                source=d.get("source"),
                uncertainty=d.get("uncertainty"),
                notes=d.get("notes"),
            )
            for slug, d in entry.properties.items()
        },
    )


# ---- class schema (declared before /materials/{id}) ----------------------


@router.get("/materials/classes", response_model=list[MaterialClassResponse])
async def list_classes(
    db: _Db, current_user: _Me, svc: _Svc, include_retired: bool = Query(default=False)
) -> list[MaterialClassResponse]:
    """List material classes + their expected-property schemas."""
    del current_user
    classes = await svc.list_classes(db, include_retired=include_retired)
    return [await _class_response(svc, db, c) for c in classes]


@router.post(
    "/materials/classes",
    response_model=MaterialClassResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_class(
    body: MaterialClassCreateRequest, db: _Db, current_user: _Me, svc: _Svc
) -> MaterialClassResponse:
    """Create a material class. Requires ``MATERIALS_CURATE``."""
    cls = await svc.create_class(
        db, actor_user_id=current_user.id, name=body.name, description=body.description
    )
    return await _class_response(svc, db, cls)


@router.get("/materials/classes/{class_id}", response_model=MaterialClassResponse)
async def get_class(class_id: int, db: _Db, current_user: _Me, svc: _Svc) -> MaterialClassResponse:
    """One material class + its schema."""
    del current_user
    cls = await svc.get_class(db, class_id)
    return await _class_response(svc, db, cls)


@router.post(
    "/materials/classes/{class_id}/properties",
    response_model=MaterialClassResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_class_property(
    class_id: int,
    body: MaterialClassPropertyCreateRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> MaterialClassResponse:
    """Add an expected property to a class. Requires ``MATERIALS_CURATE``."""
    await svc.add_class_property(
        db,
        actor_user_id=current_user.id,
        class_id=class_id,
        prop_name=body.prop_name,
        unit=body.unit,
        required=body.required,
        slug=body.slug,
    )
    cls = await svc.get_class(db, class_id)
    return await _class_response(svc, db, cls)


@router.patch(
    "/materials/classes/{class_id}/properties/{property_id}",
    response_model=MaterialClassResponse,
)
async def update_class_property(
    class_id: int,
    property_id: int,
    body: MaterialClassPropertyPatch,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> MaterialClassResponse:
    """Edit a class-property row (forward-only). Requires ``MATERIALS_CURATE``."""
    await svc.update_class_property(
        db,
        actor_user_id=current_user.id,
        class_id=class_id,
        property_id=property_id,
        prop_name=body.prop_name,
        unit=body.unit,
        required=body.required,
    )
    cls = await svc.get_class(db, class_id)
    return await _class_response(svc, db, cls)


@router.delete(
    "/materials/classes/{class_id}/properties/{property_id}",
    response_model=MaterialClassResponse,
)
async def remove_class_property(
    class_id: int,
    property_id: int,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> MaterialClassResponse:
    """Remove a class-property row (forward-only). Requires ``MATERIALS_CURATE``."""
    await svc.remove_class_property(
        db, actor_user_id=current_user.id, class_id=class_id, property_id=property_id
    )
    cls = await svc.get_class(db, class_id)
    return await _class_response(svc, db, cls)


@router.post("/materials/classes/{class_id}/retire", response_model=MaterialClassResponse)
async def retire_class(
    class_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> MaterialClassResponse:
    """Retire a class (no new materials). Requires ``MATERIALS_CURATE``."""
    cls = await svc.retire_class(db, actor_user_id=current_user.id, class_id=class_id)
    return await _class_response(svc, db, cls)


# ---- picker + approvers (static routes, before /materials/{id}) ----------


@router.get("/materials/picker", response_model=list[MaterialPickerCandidate])
async def picker(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    material_class: Annotated[str, Query(alias="class")],
    required_props: str | None = Query(default=None),
) -> list[MaterialPickerCandidate]:
    """Materials of ``class`` whose latest published version is selectable."""
    del current_user
    required = (
        [p.strip() for p in required_props.split(",") if p.strip()] if required_props else None
    )
    entries = await svc.list_for_picker(db, class_name=material_class, required_properties=required)
    return [_picker_candidate(e) for e in entries]


@router.get("/materials/approvers", response_model=list[MaterialApproverOption])
async def approvers(db: _Db, current_user: _Me, svc: _Svc) -> list[MaterialApproverOption]:
    """Active materials curators the caller can nominate (excludes self)."""
    del svc
    rows = (
        (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username)))
        .scalars()
        .all()
    )
    out: list[MaterialApproverOption] = []
    for u in rows:
        if u.id == current_user.id:
            continue
        if await user_has_permission(db, u.id, Permission.MATERIALS_CURATE):
            out.append(
                MaterialApproverOption(user_id=u.id, username=u.username, full_name=u.full_name)
            )
    return out


# ---- materials CRUD ------------------------------------------------------


@router.get("/materials", response_model=list[MaterialResponse])
async def list_materials(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    name: str | None = Query(default=None),
    class_id: int | None = Query(default=None),
    tag: str | None = Query(default=None),
    include_retired: bool = Query(default=False),
) -> list[MaterialResponse]:
    """List / search materials by name, class, and tag."""
    del current_user
    materials = await svc.search(
        db, name=name, class_id=class_id, tag=tag, include_retired=include_retired
    )
    counts = await _fetch_material_version_counts(db, [int(m.id) for m in materials])
    return [
        await _material_response(db, m, version_count=counts.get(int(m.id), 0)) for m in materials
    ]


@router.post(
    "/materials", response_model=MaterialDetailResponse, status_code=status.HTTP_201_CREATED
)
async def create_material(
    body: MaterialCreateRequest, db: _Db, current_user: _Me, svc: _Svc, notifier: _Notify
) -> MaterialDetailResponse:
    """Create a material + its first version. Requires ``MATERIALS_CURATE``."""
    approval_required = await _approval_required(db)
    approver_id, approver_name = await _resolve_approver(db, body.approver_user_id)
    material = await svc.create_material(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        class_id=body.class_id,
        properties=_specs(body.properties),
        provenance=body.provenance,
        description=body.description,
        aliases=body.aliases,
        tags=body.tags,
        confidentiality=body.confidentiality,
        reference=body.reference,
        data_pedigree=body.data_pedigree,
        conditions=body.conditions,
        version_notes=body.version_notes,
        effective_date=body.effective_date,
        approval_required=approval_required,
        designated_approver_user_id=approver_id,
        designated_approver_username=approver_name,
    )
    await _notify_material_pending(db, notifier, material.id)
    return await _detail_response(svc, db, material.id)


@router.get("/materials/{material_id}", response_model=MaterialDetailResponse)
async def get_material(
    material_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> MaterialDetailResponse:
    """One material + its full version history."""
    del current_user
    return await _detail_response(svc, db, material_id)


@router.patch("/materials/{material_id}", response_model=MaterialDetailResponse)
async def patch_material(
    material_id: int, body: MaterialMetadataPatch, db: _Db, current_user: _Me, svc: _Svc
) -> MaterialDetailResponse:
    """Edit card-level metadata (optimistic-locked). Requires ``MATERIALS_CURATE``."""
    await svc.update_material_metadata(
        db,
        actor_user_id=current_user.id,
        material_id=material_id,
        expected_version=body.expected_version,
        name=body.name,
        description=body.description,
        aliases=body.aliases,
        tags=body.tags,
        confidentiality=body.confidentiality,
    )
    return await _detail_response(svc, db, material_id)


@router.post(
    "/materials/{material_id}/versions",
    response_model=MaterialDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_version(
    material_id: int,
    body: MaterialAddVersionRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    notifier: _Notify,
) -> MaterialDetailResponse:
    """Append an immutable version. Requires ``MATERIALS_CURATE``."""
    approval_required = await _approval_required(db)
    approver_id, approver_name = await _resolve_approver(db, body.approver_user_id)
    await svc.add_version(
        db,
        actor_user_id=current_user.id,
        material_id=material_id,
        properties=_specs(body.properties),
        provenance=body.provenance,
        reference=body.reference,
        data_pedigree=body.data_pedigree,
        conditions=body.conditions,
        version_notes=body.version_notes,
        effective_date=body.effective_date,
        approval_required=approval_required,
        designated_approver_user_id=approver_id,
        designated_approver_username=approver_name,
    )
    await _notify_material_pending(db, notifier, material_id)
    return await _detail_response(svc, db, material_id)


@router.post("/materials/{material_id}/retire", response_model=MaterialResponse)
async def retire_material(
    material_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> MaterialResponse:
    """Retire a material (no delete). Requires ``MATERIALS_CURATE``."""
    material = await svc.retire(db, actor_user_id=current_user.id, material_id=material_id)
    counts = await _fetch_material_version_counts(db, [int(material.id)])
    return await _material_response(db, material, version_count=counts.get(int(material.id), 0))
