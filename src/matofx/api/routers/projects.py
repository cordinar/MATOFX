"""Projects + Scenarios router (ADR-0002).

GET    /api/projects                      → list projects visible to the actor.
POST   /api/projects                      → create a project (Creator = actor).
GET    /api/projects/{pid}                → fetch a project.
PATCH  /api/projects/{pid}                → edit name/description (OWNER).
POST   /api/projects/{pid}/archive        → archive / cancel (OWNER).
POST   /api/projects/{pid}/transfer       → transfer ownership (admin / Owner).
GET    /api/projects/{pid}/acl            → list the per-user ACL (VIEW).
POST   /api/projects/{pid}/acl            → add/replace one grant (OWNER).
PUT    /api/projects/{pid}/acl            → batch replace the ACL (OWNER).
DELETE /api/projects/{pid}/acl/{uid}      → revoke a grant (OWNER).
GET    /api/projects/{pid}/metadata       → current metadata selections (VIEW).
PUT    /api/projects/{pid}/metadata       → replace metadata selections (OWNER).
GET    /api/projects/{pid}/scenarios      → list scenarios in a project.
POST   /api/projects/{pid}/scenarios      → create a scenario in a project.
GET    /api/scenarios/{sid}               → fetch a scenario.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_current_user,
    get_db,
    get_project_service,
    get_scenario_service,
)
from matofx.api.schemas import (
    AclBatch,
    AclEntryResponse,
    AclEntryUpsert,
    ProjectAclResponse,
    ProjectCreate,
    ProjectMetadataUpdate,
    ProjectResponse,
    ProjectStateChange,
    ProjectUpdate,
    ScenarioCreate,
    ScenarioResponse,
    TransferOwnership,
    UserDirectoryEntry,
)
from matofx.hierarchy.orm import Project, Scenario
from matofx.hierarchy.service import ProjectService, ProjectWithMeta, ScenarioService
from matofx.users.orm import User

router = APIRouter()


def _project_to_response(p: Project) -> ProjectResponse:
    return ProjectResponse(
        id=p.id,
        name=p.name,
        description=p.description,
        creator_user_id=p.creator_user_id,
        state=p.state,
        version=p.version,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _project_with_meta_to_response(entry: ProjectWithMeta) -> ProjectResponse:
    """List-endpoint response carrying the resolved Owner + My-role fields."""
    p = entry.project
    return ProjectResponse(
        id=p.id,
        name=p.name,
        description=p.description,
        creator_user_id=p.creator_user_id,
        owner_user_id=entry.owner_user_id,
        owner_name=entry.owner_name,
        my_role=entry.my_role,
        state=p.state,
        version=p.version,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _scenario_to_response(s: Scenario) -> ScenarioResponse:
    return ScenarioResponse(
        id=s.id,
        project_id=s.project_id,
        name=s.name,
        description=s.description,
        created_at=s.created_at,
    )


# --- projects --------------------------------------------------------


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[ProjectResponse]:
    """List every project visible to the actor (active only by default).

    Each row carries the resolved Owner + caller's role (home-redesign
    §8.1) so the My Projects table renders both columns without a
    per-row lookup.
    """
    entries = await project_service.list_for_user_with_meta(
        db, actor_user_id=current_user.id, include_archived=include_archived
    )
    return [_project_with_meta_to_response(e) for e in entries]


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    body: ProjectCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProjectResponse:
    """Create a new project; the Creator is the authenticated user."""
    project = await project_service.create(
        db, actor_user_id=current_user.id, name=body.name, description=body.description
    )
    return _project_to_response(project)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProjectResponse:
    """Fetch one project by id."""
    project = await project_service.get(db, project_id=project_id, actor_user_id=current_user.id)
    return _project_to_response(project)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProjectResponse:
    """Edit name/description (Creator/Owner only; optimistic-lock aware)."""
    project = await project_service.update(
        db,
        project_id=project_id,
        actor_user_id=current_user.id,
        name=body.name,
        description=body.description,
        expected_version=body.expected_version,
    )
    return _project_to_response(project)


@router.post("/projects/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(
    project_id: int,
    body: ProjectStateChange,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProjectResponse:
    """Archive or cancel a project (no-delete / §D20; Creator/Owner only)."""
    project = await project_service.set_state(
        db, project_id=project_id, actor_user_id=current_user.id, state=body.state
    )
    return _project_to_response(project)


@router.post("/projects/{project_id}/transfer", response_model=ProjectResponse)
async def transfer_project(
    project_id: int,
    body: TransferOwnership,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProjectResponse:
    """Transfer ownership: successor → Owner, other non-Creator Owners revoked."""
    project = await project_service.transfer_ownership(
        db,
        project_id=project_id,
        actor_user_id=current_user.id,
        new_owner_user_id=body.new_owner_user_id,
    )
    return _project_to_response(project)


# --- user directory (for the share picker) ---------------------------


@router.get("/users/directory", response_model=list[UserDirectoryEntry])
async def list_user_directory(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[UserDirectoryEntry]:
    """The org's user directory, for the share picker and the access list.

    Returned as ``{user_id, username, full_name}``. Any authenticated user may
    read it — picking who to share with is
    an Owner action gated elsewhere; the directory itself is just the
    org's name list. Ordered by full name then username.
    """
    del current_user
    rows = (
        await db.execute(
            select(User).where(User.is_active.is_(True)).order_by(User.full_name, User.username)
        )
    ).scalars()
    return [
        UserDirectoryEntry(
            user_id=u.id,
            username=u.username,
            full_name=u.full_name or u.username,
        )
        for u in rows
    ]


# --- per-user ACL ----------------------------------------------------


@router.get("/projects/{project_id}/acl", response_model=ProjectAclResponse)
async def get_project_acl(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProjectAclResponse:
    """List the per-user ACL plus the (frozen) Creator."""
    project = await project_service.get(db, project_id=project_id, actor_user_id=current_user.id)
    entries = await project_service.list_acl(
        db, project_id=project_id, actor_user_id=current_user.id
    )
    return ProjectAclResponse(
        creator_user_id=project.creator_user_id,
        entries=[AclEntryResponse(user_id=e.user_id, role=e.role) for e in entries],
    )


@router.post("/projects/{project_id}/acl", status_code=status.HTTP_204_NO_CONTENT)
async def upsert_project_acl(
    project_id: int,
    body: AclEntryUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Add a grant, or change the role if the user is already on the ACL."""
    existing = await project_service.list_acl(
        db, project_id=project_id, actor_user_id=current_user.id
    )
    if any(e.user_id == body.user_id for e in existing):
        await project_service.update_acl_entry(
            db,
            project_id=project_id,
            user_id=body.user_id,
            role=body.role,
            actor_user_id=current_user.id,
        )
    else:
        await project_service.add_acl_entry(
            db,
            project_id=project_id,
            user_id=body.user_id,
            role=body.role,
            actor_user_id=current_user.id,
        )


@router.put("/projects/{project_id}/acl", status_code=status.HTTP_204_NO_CONTENT)
async def replace_project_acl(
    project_id: int,
    body: AclBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Replace the whole per-user ACL (the share dialog's batch save)."""
    await project_service.set_acl(
        db,
        project_id=project_id,
        grants=[(g.user_id, g.role) for g in body.grants],
        actor_user_id=current_user.id,
    )


@router.delete(
    "/projects/{project_id}/acl/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_project_acl(
    project_id: int,
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Revoke a user's grant (the Creator can't be targeted)."""
    await project_service.remove_acl_entry(
        db, project_id=project_id, user_id=user_id, actor_user_id=current_user.id
    )


# --- typed metadata --------------------------------------------------


@router.get("/projects/{project_id}/metadata")
async def get_project_metadata(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[dict[str, object]]:
    """Return the project's metadata selections (raw rows; VIEW)."""
    rows = await project_service.get_metadata(
        db, project_id=project_id, actor_user_id=current_user.id
    )
    return [
        {
            "field_id": r.field_id,
            "value_id": r.value_id,
            "value_num": float(r.value_num) if r.value_num is not None else None,
            "value_text": r.value_text,
            "value_date": r.value_date.isoformat() if r.value_date is not None else None,
        }
        for r in rows
    ]


@router.put(
    "/projects/{project_id}/metadata",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_project_metadata(
    project_id: int,
    body: ProjectMetadataUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Replace the project's metadata selections (OWNER; validated)."""
    await project_service.set_metadata(
        db,
        project_id=project_id,
        actor_user_id=current_user.id,
        selections=[s.model_dump(exclude_none=True) for s in body.selections],
    )


# --- scenarios -------------------------------------------------------


@router.get(
    "/projects/{project_id}/scenarios",
    response_model=list[ScenarioResponse],
)
async def list_scenarios(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    scenario_service: Annotated[ScenarioService, Depends(get_scenario_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ScenarioResponse]:
    """List scenarios in a project."""
    scenarios = await scenario_service.list_for_project(
        db, project_id=project_id, actor_user_id=current_user.id
    )
    return [_scenario_to_response(s) for s in scenarios]


@router.post(
    "/projects/{project_id}/scenarios",
    response_model=ScenarioResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_scenario(
    project_id: int,
    body: ScenarioCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    scenario_service: Annotated[ScenarioService, Depends(get_scenario_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScenarioResponse:
    """Create a new scenario inside a project."""
    scenario = await scenario_service.create(
        db,
        actor_user_id=current_user.id,
        project_id=project_id,
        name=body.name,
        description=body.description,
    )
    return _scenario_to_response(scenario)


@router.get("/scenarios/{scenario_id}", response_model=ScenarioResponse)
async def get_scenario(
    scenario_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    scenario_service: Annotated[ScenarioService, Depends(get_scenario_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ScenarioResponse:
    """Fetch one scenario by id."""
    scenario = await scenario_service.get(
        db, scenario_id=scenario_id, actor_user_id=current_user.id
    )
    return _scenario_to_response(scenario)
