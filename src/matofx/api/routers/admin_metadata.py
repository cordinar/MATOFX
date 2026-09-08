"""Project metadata taxonomy router (ADR-0002).

The install-wide field/value definitions behind project metadata.
Admin surface gated by ``projects.manage_metadata``; the read-only
``GET /api/metadata/fields`` is open to any authenticated user (the
project create/edit form needs the active taxonomy to render).

GET    /api/metadata/fields                       active fields + active values (any user)
GET    /api/admin/metadata/fields                 every field + value (admin)
POST   /api/admin/metadata/fields                 create a field (admin)
PATCH  /api/admin/metadata/fields/{fid}           edit / retire a field (admin)
POST   /api/admin/metadata/fields/{fid}/values    add an allowed value (admin)
PATCH  /api/admin/metadata/values/{vid}           edit / retire a value (admin)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import get_current_user, get_db, get_project_service
from matofx.api.schemas import (
    MetadataFieldCreate,
    MetadataFieldResponse,
    MetadataFieldUpdate,
    MetadataValueCreate,
    MetadataValueReorder,
    MetadataValueResponse,
    MetadataValueUpdate,
)
from matofx.hierarchy.orm import ProjectMetadataField, ProjectMetadataValue
from matofx.hierarchy.service import ProjectService
from matofx.users.orm import User

router = APIRouter()


def _field_to_response(
    f: ProjectMetadataField, values: list[ProjectMetadataValue]
) -> MetadataFieldResponse:
    return MetadataFieldResponse(
        id=f.id,
        key=f.key,
        label=f.label,
        field_type=f.field_type,
        number_format=f.number_format,
        format_mask=f.format_mask,
        required=f.required,
        display_order=f.display_order,
        show_in_table=f.show_in_table,
        active=f.active,
        values=[MetadataValueResponse(id=v.id, value=v.value, active=v.active) for v in values],
    )


@router.get("/metadata/fields", response_model=list[MetadataFieldResponse])
async def list_active_metadata_fields(
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[MetadataFieldResponse]:
    """Active fields + active values — for the project create/edit form."""
    del current_user
    rows = await project_service.list_active_metadata_fields(db)
    return [_field_to_response(f, vs) for f, vs in rows]


@router.get("/admin/metadata/fields", response_model=list[MetadataFieldResponse])
async def admin_list_metadata_fields(
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[MetadataFieldResponse]:
    """Every field (incl. retired) + every value. Admin-gated."""
    rows = await project_service.admin_list_metadata_fields(db, actor_user_id=current_user.id)
    return [_field_to_response(f, vs) for f, vs in rows]


@router.post(
    "/admin/metadata/fields",
    response_model=MetadataFieldResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_metadata_field(
    body: MetadataFieldCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MetadataFieldResponse:
    """Create a metadata field."""
    field = await project_service.admin_create_metadata_field(
        db,
        actor_user_id=current_user.id,
        key=body.key,
        label=body.label,
        field_type=body.field_type,
        required=body.required,
        display_order=body.display_order,
        show_in_table=body.show_in_table,
        number_format=body.number_format,
        format_mask=body.format_mask,
    )
    return _field_to_response(field, [])


@router.patch("/admin/metadata/fields/{field_id}", response_model=MetadataFieldResponse)
async def update_metadata_field(
    field_id: int,
    body: MetadataFieldUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MetadataFieldResponse:
    """Edit or retire a metadata field (``key``/``field_type`` immutable)."""
    field = await project_service.admin_update_metadata_field(
        db,
        actor_user_id=current_user.id,
        field_id=field_id,
        label=body.label,
        required=body.required,
        display_order=body.display_order,
        show_in_table=body.show_in_table,
        number_format=body.number_format,
        format_mask=body.format_mask,
        active=body.active,
    )
    return _field_to_response(field, [])


@router.post(
    "/admin/metadata/fields/{field_id}/values",
    response_model=MetadataValueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_metadata_value(
    field_id: int,
    body: MetadataValueCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MetadataValueResponse:
    """Add an allowed value to a select field."""
    row = await project_service.admin_create_metadata_value(
        db,
        actor_user_id=current_user.id,
        field_id=field_id,
        value=body.value,
        display_order=body.display_order,
    )
    return MetadataValueResponse(id=row.id, value=row.value, active=row.active)


@router.patch("/admin/metadata/values/{value_id}", response_model=MetadataValueResponse)
async def update_metadata_value(
    value_id: int,
    body: MetadataValueUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MetadataValueResponse:
    """Rename, reorder, or retire a metadata value."""
    row = await project_service.admin_update_metadata_value(
        db,
        actor_user_id=current_user.id,
        value_id=value_id,
        value=body.value,
        display_order=body.display_order,
        active=body.active,
    )
    return MetadataValueResponse(id=row.id, value=row.value, active=row.active)


@router.patch(
    "/admin/metadata/fields/{field_id}/values/order",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reorder_metadata_values(
    field_id: int,
    body: MetadataValueReorder,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Set the display order of a select field's values (admin)."""
    await project_service.admin_reorder_metadata_values(
        db,
        actor_user_id=current_user.id,
        field_id=field_id,
        ordered_value_ids=list(body.value_ids),
    )
