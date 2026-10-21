"""Admin router — privileged surfaces for the Admin Space pages.

Stage 1: users + roles. Stage 2: teams + tools. File-paths
(StorageLocation / PythonEnv / RuntimeSettings) lands in stage 3.

Every endpoint enforces a permission slug from
:mod:`matofx.users.permissions`. The "actor" passed into each
service call is the bearer-token user, so audit rows carry the right
identity.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    enforce_password_complexity,
    get_aos_hosts_service,
    get_current_user,
    get_db,
    get_hpc_clusters_service,
    get_python_envs_service,
    get_role_service,
    get_runtime_settings_service,
    get_secrets_store,
    get_settings,
    get_storage_locations_service,
    get_team_service,
    get_tool_service,
    get_user_service,
)
from matofx.api.schemas import (
    AdminActiveSessionRow,
    AdminActiveSessionsResponse,
    AdminAosHostCreate,
    AdminAosHostResponse,
    AdminAosHostUpdate,
    AdminArchiveRequest,
    AdminHpcClusterCreate,
    AdminHpcClusterResponse,
    AdminHpcClusterUpdate,
    AdminLicenceCapacityResponse,
    AdminMembersBulkAddRequest,
    AdminPermissionInfo,
    AdminPromoteRequest,
    AdminPythonEnvCreate,
    AdminPythonEnvResponse,
    AdminRoleCreate,
    AdminRoleDescriptionUpdate,
    AdminRolePermissionsSet,
    AdminRoleRenameRequest,
    AdminRoleResponse,
    AdminRuntimeSettingsResponse,
    AdminRuntimeSettingValue,
    AdminSetApproverRequest,
    AdminSetRoleRequest,
    AdminStorageLocationCreate,
    AdminStorageLocationResponse,
    AdminTeamCreate,
    AdminTeamMemberAddRequest,
    AdminTeamMemberResponse,
    AdminTeamResponse,
    AdminTeamUpdate,
    AdminToolDescriptorUpdate,
    AdminToolDetailResponse,
    AdminToolFileContent,
    AdminToolFileEntry,
    AdminToolFileList,
    AdminToolFileSaveRequest,
    AdminToolMetadataPayload,
    AdminToolResponse,
    AdminToolSetApprovers,
    AdminToolVersionEntry,
    AdminToolVersionList,
    AdminUserCreate,
    AdminUserIdentityUpdate,
    AdminUserPasswordSet,
    AdminUserResponse,
    AdminUserSetCapabilityRoles,
    AdminUserSetTeams,
    AdminUserTeamMembership,
    ComplianceReportGeneratedResponse,
    ComplianceReportGenerateRequest,
    ComplianceReportListResponse,
    ComplianceReportRow,
    IntegrityAcknowledgeRequest,
    SiteModeResponse,
    ToolDraftList,
    ToolDraftResponse,
    ToolDraftSaveRequest,
    ToolDraftSummary,
    ToolPythonEnvRequest,
    ToolRunCapRequest,
)
from matofx.audit import RecordInput
from matofx.audit.orm import AuditLog
from matofx.config import Settings
from matofx.runner.admin_service import RuntimeSettingsService
from matofx.secrets import SecretNotFoundError, SecretsError, SecretsStore
from matofx.tools.descriptor import (
    LicenceRequirement,
    ToolDescriptor,
    tool_descriptions,
)
from matofx.tools.exec_orm import AosHost, HpcCluster
from matofx.tools.hosts_service import AosHostsService, HpcClustersService
from matofx.tools.orm import Tool, ToolDraft, ToolVersion
from matofx.tools.paths_service import PythonEnvsService, StorageLocationsService
from matofx.tools.service import GuideImageEdit, ToolService
from matofx.tools.staging import BundleStagingError, stage_uploaded_bundle
from matofx.tools.versioning import (
    INITIAL_VERSION,
    bump_major,
    headline_for_archive,
    headline_for_unarchive,
)
from matofx.users.orm import AuthSession, User
from matofx.users.permissions import (
    PERMISSION_DESCRIPTIONS,
    Permission,
    is_capability_permission,
    user_has_permission,
)
from matofx.users.role_orm import Role, UserCapabilityRole
from matofx.users.role_service import RoleService
from matofx.users.service import UserService
from matofx.users.team_orm import Team, UserTeam
from matofx.users.team_service import TeamService

router = APIRouter()


# ---------- gates ---------------------------------------------------------


async def _require(session: AsyncSession, actor_user_id: int, permission: Permission) -> None:
    """Raise 403 if ``actor_user_id`` doesn't hold ``permission``."""
    if not await user_has_permission(session, actor_user_id, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"You don't have permission to do that ({permission.value}). "
                "Ask an administrator if you need access."
            ),
        )


# ---------- FAL assignment gate (ADR-0019 slice 5) ------------------------
#
# Under a Floating Access Licence the metered act is an administrator
# *granting* a person access — creating an active account or reactivating
# a deactivated one. Revoking is never gated: an install must always be
# able to get back under its cap.


async def _gate_assignment(
    request: Request,
    db: AsyncSession,
    settings: Settings,
    *,
    actor_user_id: int,
    target_username: str,
    target_user_id: int | None = None,
) -> None:
    """Raise when the licence has no floating assignment left."""
    from matofx.licence import admit_assignment, resolve_licence  # noqa: PLC0415
    from matofx.licence.errors import LicenceAssignmentsExhaustedError  # noqa: PLC0415

    decision = await admit_assignment(
        db,
        resolution=resolve_licence(settings),
        audit=request.app.state.audit,
        actor=f"user:{actor_user_id}",
        target_username=target_username,
        target_user_id=target_user_id,
    )
    if not decision.admitted:
        raise LicenceAssignmentsExhaustedError(
            decision.message or "No licence places are available."
        )


async def _release_assignment(
    request: Request,
    db: AsyncSession,
    settings: Settings,
    *,
    actor_user_id: int,
    target_username: str,
    target_user_id: int,
) -> None:
    """Record a freed floating assignment (no-op unless FAL)."""
    from matofx.licence import release_assignment, resolve_licence  # noqa: PLC0415

    await release_assignment(
        db,
        resolution=resolve_licence(settings),
        audit=request.app.state.audit,
        actor=f"user:{actor_user_id}",
        target_username=target_username,
        target_user_id=target_user_id,
    )


async def _user_to_response(session: AsyncSession, user: User) -> AdminUserResponse:
    """Render a :class:`User` ORM row as the admin API response."""
    role_name = ""
    if user.role_id is not None:
        role = await session.get(Role, user.role_id)
        if role is not None:
            role_name = role.name
    # Bundle team membership inline so the edit page renders without
    # an extra list-teams round-trip.
    team_stmt = (
        select(Team.id, Team.name)
        .join(UserTeam, UserTeam.team_id == Team.id)
        .where(UserTeam.user_id == user.id)
        .order_by(Team.name)
    )
    teams = [
        AdminUserTeamMembership(id=row.id, name=row.name)
        for row in (await session.execute(team_stmt)).all()
    ]
    # Bundle capability-role membership (ADR-0005) inline, same reason.
    cap_stmt = (
        select(Role.name)
        .join(UserCapabilityRole, UserCapabilityRole.role_id == Role.id)
        .where(UserCapabilityRole.user_id == user.id)
        .order_by(Role.name)
    )
    capability_roles = list((await session.execute(cap_stmt)).scalars().all())
    return AdminUserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=user.full_name,
        role=role_name,
        is_active=user.is_active,
        is_approver=user.is_approver,
        teams=teams,
        capability_roles=capability_roles,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        logon_granted_at=user.logon_granted_at,
        logon_revoked_at=user.logon_revoked_at,
    )


# ---------- users ---------------------------------------------------------


@router.get("/admin/users", response_model=list[AdminUserResponse])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminUserResponse]:
    """List every user. Requires ``users.manage``."""
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    users = await user_service.list_all(db)
    return [await _user_to_response(db, u) for u in users]


@router.post(
    "/admin/users",
    response_model=AdminUserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    body: AdminUserCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdminUserResponse:
    """Create a user. Requires ``users.create_with_role``.

    Identity is captured as split first / last name; ``full_name`` is
    derived as ``"{first} {last}".strip()`` when the body doesn't
    supply one explicitly. ``team_ids``, when non-empty, additionally
    requires ``teams.manage`` and assigns the user to each team after
    creation (each assignment audited individually). ``capability_roles``
    (ADR-0005), when non-empty, additionally requires ``roles.manage``
    and stacks each capability role on the new account in the same
    request transaction — a bad role name rolls the whole creation back.
    """
    await _require(db, current_user.id, Permission.USERS_CREATE_WITH_ROLE)
    if body.capability_roles:
        await _require(db, current_user.id, Permission.ROLES_MANAGE)
    await enforce_password_complexity(db, body.password)
    # A new account is active on creation, so it consumes an assignment.
    # Gated before creation so a refusal leaves nothing behind.
    await _gate_assignment(
        request, db, settings, actor_user_id=current_user.id, target_username=body.username
    )
    derived_full_name = (
        body.full_name if body.full_name else f"{body.first_name} {body.last_name}".strip()
    )
    user = await user_service.create_user(
        db,
        username=body.username,
        email=body.email,
        password=body.password,
        first_name=body.first_name,
        last_name=body.last_name,
        full_name=derived_full_name or None,
        role=body.role,
        is_approver=body.is_approver,
        actor=f"user:{current_user.id}",
        actor_user_id=current_user.id,
    )
    if body.team_ids:
        await team_service.set_user_teams(
            db,
            actor_user_id=current_user.id,
            user_id=user.id,
            team_ids=body.team_ids,
        )
    if body.capability_roles:
        await role_service.set_user_capability_roles(
            db,
            actor_user_id=current_user.id,
            target_user_id=user.id,
            role_names=body.capability_roles,
        )
    return await _user_to_response(db, user)


@router.get("/admin/users/{user_id}", response_model=AdminUserResponse)
async def get_user(
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Fetch one user. Requires ``users.manage``."""
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="We couldn't find that user.",
        )
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/deactivate", response_model=AdminUserResponse)
async def deactivate_user(
    user_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdminUserResponse:
    """Deactivate a user. Requires ``users.manage``.

    Under a Floating Access Licence this frees the account's licence
    place. Never gated — getting back under a cap must always work.
    """
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    target = await user_service.get_by_id(db, user_id=user_id)
    await user_service.deactivate(
        db,
        user_id=user_id,
        actor=f"user:{current_user.id}",
        actor_user_id=current_user.id,
    )
    if target is not None:
        await _release_assignment(
            request,
            db,
            settings,
            actor_user_id=current_user.id,
            target_username=target.username,
            target_user_id=target.id,
        )
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/reactivate", response_model=AdminUserResponse)
async def reactivate_user(
    user_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdminUserResponse:
    """Re-enable a previously deactivated user. Requires ``users.manage``.

    Under a Floating Access Licence this is the metered act — re-enabling
    an account gives it back a licence place (ADR-0019 slice 5).
    """
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    target = await user_service.get_by_id(db, user_id=user_id)
    if target is not None and not target.is_active:
        await _gate_assignment(
            request,
            db,
            settings,
            actor_user_id=current_user.id,
            target_username=target.username,
            target_user_id=target.id,
        )
    await user_service.reactivate(
        db,
        user_id=user_id,
        actor=f"user:{current_user.id}",
        actor_user_id=current_user.id,
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/set-approver", response_model=AdminUserResponse)
async def set_approver(
    user_id: int,
    body: AdminSetApproverRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Toggle the ``is_approver`` flag. Requires ``users.manage``."""
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    await user_service.set_approver(
        db,
        user_id=user_id,
        is_approver=body.is_approver,
        actor=f"user:{current_user.id}",
        actor_user_id=current_user.id,
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/set-role", response_model=AdminUserResponse)
async def set_role(
    user_id: int,
    body: AdminSetRoleRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Assign a different role. Requires ``roles.manage``."""
    await _require(db, current_user.id, Permission.ROLES_MANAGE)
    await role_service.assign_role_to_user(
        db,
        actor_user_id=current_user.id,
        target_user_id=user_id,
        role_name=body.role,
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/update-identity", response_model=AdminUserResponse)
async def update_user_identity(
    user_id: int,
    body: AdminUserIdentityUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Edit identity fields (first / last name, email). Requires ``users.manage``."""
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    await user_service.update_identity(
        db,
        user_id=user_id,
        first_name=body.first_name,
        last_name=body.last_name,
        full_name=body.full_name or None,
        email=body.email,
        actor=f"user:{current_user.id}",
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    assert user is not None  # noqa: S101 — refetched immediately after update
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/set-password", response_model=AdminUserResponse)
async def set_user_password(
    user_id: int,
    body: AdminUserPasswordSet,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Admin-initiated password reset. Requires ``users.manage``.

    The submitted password is run through ``validate_password``;
    weak passwords are rejected with a friendly message rather than
    silently accepted.
    """
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    await enforce_password_complexity(db, body.password)
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    await user_service.set_password(
        db,
        user_id=user_id,
        password=body.password,
        actor=f"user:{current_user.id}",
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    assert user is not None  # noqa: S101 — refetched immediately after set
    return await _user_to_response(db, user)


@router.post("/admin/users/{user_id}/set-teams", response_model=AdminUserResponse)
async def set_user_teams(
    user_id: int,
    body: AdminUserSetTeams,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Bulk-replace the user's team membership. Requires ``teams.manage``."""
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    await team_service.set_user_teams(
        db,
        actor_user_id=current_user.id,
        user_id=user_id,
        team_ids=body.team_ids,
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    assert user is not None  # noqa: S101 — refetched immediately after set
    return await _user_to_response(db, user)


@router.post(
    "/admin/users/{user_id}/set-capability-roles",
    response_model=AdminUserResponse,
)
async def set_user_capability_roles(
    user_id: int,
    body: AdminUserSetCapabilityRoles,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserResponse:
    """Bulk-replace a user's capability roles (ADR-0005). Requires ``roles.manage``.

    Only ``capability``-kind role names are accepted; a base-role name
    returns 400. The base role itself is changed via /set-role.
    """
    await _require(db, current_user.id, Permission.ROLES_MANAGE)
    user = await user_service.get_by_id(db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that user."
        )
    await role_service.set_user_capability_roles(
        db,
        actor_user_id=current_user.id,
        target_user_id=user_id,
        role_names=body.role_names,
    )
    user = await user_service.get_by_id(db, user_id=user_id)
    assert user is not None  # noqa: S101 — refetched immediately after set
    return await _user_to_response(db, user)


# ---------- roles ---------------------------------------------------------


@router.get("/admin/roles", response_model=list[AdminRoleResponse])
async def list_roles(
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminRoleResponse]:
    """List every role plus its permission set + member count. Requires ``users.manage``."""
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    roles = await role_service.list_roles(db)
    # Two grouped queries for member counts so the listing stays O(1) on
    # the number of roles. Base-role membership lives on ``User.role_id``;
    # capability membership lives on the ``user_role`` join (ADR-0005), so
    # each role's count comes from the source matching its kind.
    base_counts_stmt = sa.select(User.role_id, sa.func.count(User.id)).group_by(User.role_id)
    base_counts = {row[0]: int(row[1]) for row in (await db.execute(base_counts_stmt)).all()}
    cap_counts_stmt = sa.select(
        UserCapabilityRole.role_id, sa.func.count(UserCapabilityRole.user_id)
    ).group_by(UserCapabilityRole.role_id)
    cap_counts = {row[0]: int(row[1]) for row in (await db.execute(cap_counts_stmt)).all()}
    out: list[AdminRoleResponse] = []
    for role in roles:
        permissions = sorted(await role_service.get_permissions(db, role.id))
        counts = cap_counts if role.kind == "capability" else base_counts
        out.append(
            AdminRoleResponse(
                id=role.id,
                name=role.name,
                description=role.description,
                is_built_in=role.is_built_in,
                kind=role.kind,
                permissions=permissions,
                user_count=int(counts.get(role.id, 0)),
            )
        )
    return out


@router.post(
    "/admin/roles",
    response_model=AdminRoleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_role(
    body: AdminRoleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRoleResponse:
    """Create a custom role with the supplied permission set.

    Requires ``roles.manage``. Built-in role names
    (``super_user``, ``administrator``, ``beta_tester``,
    ``regular_user``) are reserved — using one returns 409.
    """
    role = await role_service.create_role(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        description=body.description,
        permissions=body.permissions,
        kind=body.kind,
    )
    permissions = sorted(await role_service.get_permissions(db, role.id))
    return AdminRoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        is_built_in=role.is_built_in,
        kind=role.kind,
        permissions=permissions,
        user_count=0,
    )


@router.post("/admin/roles/{role_id}/set-permissions", response_model=AdminRoleResponse)
async def set_role_permissions(
    role_id: int,
    body: AdminRolePermissionsSet,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRoleResponse:
    """Replace a role's permission set. Requires ``roles.manage``.

    The ``super_user`` role is sentinel-managed (always grants every
    permission); attempting to edit it returns 403.
    """
    await role_service.set_permissions(
        db,
        actor_user_id=current_user.id,
        role_id=role_id,
        permissions=body.permissions,
    )
    return await _role_to_response(db, role_service, role_id)


@router.post("/admin/roles/{role_id}/update-description", response_model=AdminRoleResponse)
async def update_role_description(
    role_id: int,
    body: AdminRoleDescriptionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRoleResponse:
    """Edit a role's description (the *name* is locked for built-ins). Requires ``roles.manage``."""
    await role_service.update_description(
        db,
        actor_user_id=current_user.id,
        role_id=role_id,
        description=body.description,
    )
    return await _role_to_response(db, role_service, role_id)


@router.post("/admin/roles/{role_id}/rename", response_model=AdminRoleResponse)
async def rename_role(
    role_id: int,
    body: AdminRoleRenameRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRoleResponse:
    """Rename a custom role. Requires ``roles.manage``.

    Built-in role names are locked (renaming one, or colliding with a
    built-in name, returns 409); a clash with any other existing role
    also returns 409.
    """
    await role_service.rename_role(
        db,
        actor_user_id=current_user.id,
        role_id=role_id,
        name=body.name,
    )
    return await _role_to_response(db, role_service, role_id)


@router.post("/admin/roles/{role_id}/members/bulk", response_model=AdminRoleResponse)
async def add_role_members(
    role_id: int,
    body: AdminMembersBulkAddRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRoleResponse:
    """Grant a capability role to many users at once. Requires ``roles.manage``.

    Only ``capability``-kind roles can take members this way — a user's
    single base role is set per-user via ``/admin/users/{id}/set-role``,
    so a base role here returns 400. Users already holding the capability
    are skipped silently.
    """
    await role_service.add_users_to_capability(
        db,
        actor_user_id=current_user.id,
        role_id=role_id,
        user_ids=body.user_ids,
    )
    return await _role_to_response(db, role_service, role_id)


@router.delete("/admin/roles/{role_id}/members/{user_id}", response_model=AdminRoleResponse)
async def remove_role_member(
    role_id: int,
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRoleResponse:
    """Revoke one capability grant from a single user. Requires ``roles.manage``.

    Only ``capability``-kind roles hold members this way — a user's single
    base role is changed via ``/admin/users/{id}/set-role``, so a base role
    here returns 400. Revoking a capability the user doesn't hold is a no-op.
    """
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that role."
        )
    await role_service.revoke_capability_role(
        db,
        actor_user_id=current_user.id,
        target_user_id=user_id,
        role_name=role.name,
    )
    return await _role_to_response(db, role_service, role_id)


@router.delete("/admin/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    role_service: Annotated[RoleService, Depends(get_role_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Always refused (409) — access-rights groups are kept permanently for audit."""
    await role_service.delete_role(
        db,
        actor_user_id=current_user.id,
        role_id=role_id,
    )


@router.get("/admin/permissions", response_model=list[AdminPermissionInfo])
async def list_permissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminPermissionInfo]:
    """Return the closed permission catalog with labels + descriptions.

    Powers the permission editor on the admin page so the operator
    sees every gate point without us having to mirror the catalog
    in the frontend code.
    """
    await _require(db, current_user.id, Permission.USERS_MANAGE)
    out: list[AdminPermissionInfo] = []
    for member in Permission:
        out.append(
            AdminPermissionInfo(
                slug=member.value,
                label=_permission_label(member.value),
                description=PERMISSION_DESCRIPTIONS.get(member, ""),
                is_capability=is_capability_permission(member),
            )
        )
    return out


# Whole-section label overrides (renames + spelled-out acronyms) used when
# turning a slug into a human label, e.g. ``vv.curate`` → ``V&V: curate``.
_SECTION_LABEL_OVERRIDES: dict[str, str] = {
    "vv": "V&V",
    "library": "File library",
    "models": "Surrogate models",
}
# Per-token acronyms applied when no whole-section override matches, e.g.
# ``hpc_clusters`` → ``HPC clusters``.
_SECTION_TOKEN_ACRONYMS: dict[str, str] = {"hpc": "HPC", "aos": "AOS", "kpi": "KPI"}


def _humanise_section(section: str) -> str:
    """Render a slug's section prefix as a human label (acronym-aware)."""
    if section in _SECTION_LABEL_OVERRIDES:
        return _SECTION_LABEL_OVERRIDES[section]
    return " ".join(
        _SECTION_TOKEN_ACRONYMS.get(token, token.title()) for token in section.split("_")
    )


def _permission_label(slug: str) -> str:
    """Render ``projects.see_all`` as ``Projects: See all`` (acronym/rename-aware)."""
    section, _, leaf = slug.partition(".")
    return f"{_humanise_section(section)}: {leaf.replace('_', ' ')}"


async def _role_to_response(
    session: AsyncSession,
    role_service: RoleService,
    role_id: int,
) -> AdminRoleResponse:
    """Re-render a role row after a mutation (permissions + member count)."""
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that role."
        )
    permissions = sorted(await role_service.get_permissions(session, role.id))
    # Membership lives in different places per kind: a base role's members
    # are users whose single ``role_id`` points here; a capability's are
    # rows in the ``user_role`` join (ADR-0005). Count from the right one
    # so a capability doesn't always report 0.
    if role.kind == "capability":
        count_stmt = sa.select(sa.func.count(UserCapabilityRole.user_id)).where(
            UserCapabilityRole.role_id == role_id
        )
    else:
        count_stmt = sa.select(sa.func.count(User.id)).where(User.role_id == role_id)
    user_count = int((await session.execute(count_stmt)).scalar_one())
    return AdminRoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        is_built_in=role.is_built_in,
        kind=role.kind,
        permissions=permissions,
        user_count=user_count,
    )


# ---------- teams ---------------------------------------------------------


@router.get("/admin/teams", response_model=list[AdminTeamResponse])
async def list_teams(
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminTeamResponse]:
    """List every team + its member count. Requires ``teams.manage``."""
    await _require(db, current_user.id, Permission.TEAMS_MANAGE)
    teams = await team_service.list_teams(db)
    out: list[AdminTeamResponse] = []
    for team in teams:
        members = await team_service.list_team_members(db, team.id)
        out.append(
            AdminTeamResponse(
                id=team.id,
                name=team.name,
                description=team.description,
                member_count=len(members),
            )
        )
    return out


@router.post(
    "/admin/teams",
    response_model=AdminTeamResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_team(
    body: AdminTeamCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminTeamResponse:
    """Create a new team. Requires ``teams.manage``."""
    team = await team_service.create_team(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        description=body.description,
    )
    return AdminTeamResponse(
        id=team.id, name=team.name, description=team.description, member_count=0
    )


@router.put("/admin/teams/{team_id}", response_model=AdminTeamResponse)
async def update_team(
    team_id: int,
    body: AdminTeamUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminTeamResponse:
    """Rename a team and/or rewrite its description. Requires ``teams.manage``.

    A name clash with another team returns 409. Memberships are
    untouched.
    """
    team = await team_service.update_team(
        db,
        actor_user_id=current_user.id,
        team_id=team_id,
        name=body.name,
        description=body.description,
    )
    members = await team_service.list_team_members(db, team.id)
    return AdminTeamResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        member_count=len(members),
    )


@router.get(
    "/admin/teams/{team_id}/members",
    response_model=list[AdminTeamMemberResponse],
)
async def list_team_members(
    team_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminTeamMemberResponse]:
    """List the users in a team. Requires ``teams.manage``."""
    await _require(db, current_user.id, Permission.TEAMS_MANAGE)
    members = await team_service.list_team_members(db, team_id)
    return [
        AdminTeamMemberResponse(user_id=m.id, username=m.username, full_name=m.full_name or "")
        for m in members
    ]


@router.post("/admin/teams/{team_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_team_member(
    team_id: int,
    body: AdminTeamMemberAddRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Add a user to a team. Requires ``teams.manage``."""
    await team_service.add_user_to_team(
        db,
        actor_user_id=current_user.id,
        user_id=body.user_id,
        team_id=team_id,
    )


@router.post("/admin/teams/{team_id}/members/bulk", status_code=status.HTTP_204_NO_CONTENT)
async def add_team_members(
    team_id: int,
    body: AdminMembersBulkAddRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Add several users to a team in one call. Requires ``teams.manage``.

    Users already in the team are skipped silently rather than erroring.
    """
    await team_service.add_users_to_team(
        db,
        actor_user_id=current_user.id,
        team_id=team_id,
        user_ids=body.user_ids,
    )


@router.delete(
    "/admin/teams/{team_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_team_member(
    team_id: int,
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Remove a user from a team. Requires ``teams.manage``."""
    await team_service.remove_user_from_team(
        db,
        actor_user_id=current_user.id,
        user_id=user_id,
        team_id=team_id,
    )


# ---------- tools ---------------------------------------------------------


@router.get("/admin/tools", response_model=list[AdminToolResponse])
async def list_admin_tools(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminToolResponse]:
    """List every tool incl. archived. Requires ``tools.install``.

    Distinct from the regular ``GET /api/tools`` which filters by
    state-visibility; admins see everything.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    rows = list((await db.execute(select(Tool).order_by(Tool.name))).scalars())
    return [_tool_to_admin_response(t) for t in rows]


@router.get("/admin/tools/{alias}", response_model=AdminToolDetailResponse)
async def get_admin_tool(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolDetailResponse:
    """Fetch one tool with its full descriptor — drives the Edit Tool form."""
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=True
    )
    descriptor_dict = tool.descriptor if isinstance(tool.descriptor, dict) else {}
    short_description, long_description = tool_descriptions(descriptor_dict)
    tool_version_id = (
        await db.execute(
            sa.select(ToolVersion.id)
            .where(ToolVersion.alias == tool.alias, ToolVersion.version == tool.version)
            .limit(1)
        )
    ).scalar_one_or_none()
    return AdminToolDetailResponse(
        alias=tool.alias,
        name=tool.name,
        version=tool.version,
        state=tool.state,
        short_description=short_description,
        long_description=long_description,
        archived_at=tool.archived_at,
        bundle_path=tool.bundle_path,
        descriptor=descriptor_dict,
        run_cap=tool.run_cap,
        python_env_id=tool.python_env_id,
        tool_version_id=tool_version_id,
        teams_pending=bool(tool.teams_pending),
    )


@router.get(
    "/admin/tools/{alias}/versions",
    response_model=AdminToolVersionList,
)
async def list_tool_versions(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolVersionList:
    """Return the tool's snapshot history, newest first.

    Drives the change-log modal on the admin Tools page. Combines the
    immutable ``tool_version`` snapshots (install / edit / replace /
    release) with **archive and re-activate events** read from the audit
    trail — those don't change the version, so they can't have their own
    ``(alias, version)`` snapshot row, but they belong in the log. Each
    entry's actor is pre-resolved to ``"Full Name (username)"`` so the UI
    doesn't need a second list-users call. Entries are immutable.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    rows = list(
        (
            await db.execute(
                sa.select(ToolVersion)
                .where(ToolVersion.alias == alias)
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
            )
        ).scalars()
    )

    # Archive / unarchive events live in the audit trail, keyed on the
    # tool row's id. Fetch the tool to resolve that id; absence just means
    # "no lifecycle events to merge".
    tool = (
        await db.execute(sa.select(Tool).where(Tool.alias == alias).limit(1))
    ).scalar_one_or_none()
    audit_rows: list[AuditLog] = []
    if tool is not None:
        audit_rows = list(
            (
                await db.execute(
                    sa.select(AuditLog)
                    .where(
                        AuditLog.resource_type == "tool",
                        AuditLog.resource_id == str(tool.id),
                        AuditLog.action.in_(["tools.tool.archive", "tools.tool.unarchive"]),
                    )
                    .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
                )
            ).scalars()
        )

    if not rows and not audit_rows:
        return AdminToolVersionList(versions=[])

    # One label lookup covering both snapshot authors and audit actors
    # (``"user:<id>"``).
    user_ids: set[int] = {row.created_by_user_id for row in rows}
    for a in audit_rows:
        if a.actor.startswith("user:"):
            with contextlib.suppress(ValueError):
                user_ids.add(int(a.actor.split(":", 1)[1]))
    label_by_id: dict[int, str] = {}
    if user_ids:
        user_stmt = sa.select(User.id, User.full_name, User.username).where(User.id.in_(user_ids))
        for uid, full_name, username in (await db.execute(user_stmt)).all():
            full = (full_name or "").strip()
            username_str = str(username or "")
            label_by_id[int(uid)] = f"{full} ({username_str})" if full else username_str

    entries = [
        AdminToolVersionEntry(
            id=row.id,
            version=row.version,
            created_at=row.created_at,
            created_by_label=label_by_id.get(
                row.created_by_user_id, f"user:{row.created_by_user_id}"
            ),
            change_summary=row.change_summary,
            changes=dict(row.changes) if isinstance(row.changes, dict) else {},
        )
        for row in rows
    ]
    for a in audit_rows:
        payload = a.payload if isinstance(a.payload, dict) else {}
        version = str(payload.get("version") or (tool.version if tool else ""))
        actor_label = a.actor
        if a.actor.startswith("user:"):
            with contextlib.suppress(ValueError):
                actor_label = label_by_id.get(int(a.actor.split(":", 1)[1]), a.actor)
        if a.action == "tools.tool.archive":
            reason = payload.get("reason")
            summary = headline_for_archive(version, reason if isinstance(reason, str) else None)
            changes: dict[str, Any] = {"archived": {"old": False, "new": True}}
            if isinstance(reason, str) and reason.strip():
                changes["archive_reason"] = {"old": None, "new": reason.strip()}
        else:
            summary = headline_for_unarchive(version)
            changes = {"archived": {"old": True, "new": False}}
        entries.append(
            AdminToolVersionEntry(
                # Offset the audit id so it can't collide with a
                # tool_version id in the merged list's UI keys.
                id=-int(a.id),
                version=version,
                created_at=a.timestamp,
                created_by_label=actor_label,
                change_summary=summary,
                changes=changes,
            )
        )

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return AdminToolVersionList(versions=entries)


# ---------- new-tool drafts ----------------------------------------------
#
# Drafts let an author save partial work on the New Tool form and resume
# it later. Private to the owner, gated on the same ``tools.install``
# permission as installing a tool. They store the typed metadata + wizard
# state only — never the uploaded bundle files.


async def _tool_draft_to_summary(row: ToolDraft) -> ToolDraftSummary:
    return ToolDraftSummary(
        id=row.id,
        title=row.title or "",
        alias=row.alias or "",
        updated_at=row.updated_at,
    )


async def _fetch_owned_draft(db: AsyncSession, *, draft_id: int, owner_user_id: int) -> ToolDraft:
    """Load a draft, 404ing when missing or owned by someone else.

    Existence-hide: another user's draft 404s rather than 403s, so the
    inspector can't enumerate who has drafts in flight.
    """
    row = await db.get(ToolDraft, draft_id)
    if row is None or row.owner_user_id != owner_user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That draft doesn't exist (or isn't yours).",
        )
    return row


@router.get("/admin/tool-drafts", response_model=ToolDraftList)
async def list_tool_drafts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ToolDraftList:
    """List the caller's own New Tool drafts, newest-edited first."""
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    rows = list(
        (
            await db.execute(
                sa.select(ToolDraft)
                .where(ToolDraft.owner_user_id == current_user.id)
                .order_by(ToolDraft.updated_at.desc(), ToolDraft.id.desc())
            )
        ).scalars()
    )
    return ToolDraftList(drafts=[await _tool_draft_to_summary(r) for r in rows])


@router.get("/admin/tool-drafts/{draft_id}", response_model=ToolDraftResponse)
async def get_tool_draft(
    draft_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ToolDraftResponse:
    """Return one of the caller's drafts in full, for resuming the form."""
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    row = await _fetch_owned_draft(db, draft_id=draft_id, owner_user_id=current_user.id)
    return ToolDraftResponse(
        id=row.id,
        title=row.title or "",
        alias=row.alias or "",
        payload=dict(row.payload) if isinstance(row.payload, dict) else {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("/admin/tool-drafts", response_model=ToolDraftResponse)
async def save_tool_draft(
    body: ToolDraftSaveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ToolDraftResponse:
    """Create a new draft, or update the caller's existing one in place.

    When ``draft_id`` is supplied the matching owned draft is overwritten
    (so repeated "Save draft" clicks don't pile up rows); otherwise a
    fresh draft is created.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    payload = dict(body.payload) if isinstance(body.payload, dict) else {}
    if body.draft_id is not None:
        row = await _fetch_owned_draft(db, draft_id=body.draft_id, owner_user_id=current_user.id)
        row.title = body.title or ""
        row.alias = body.alias or ""
        row.payload = payload
    else:
        row = ToolDraft(
            owner_user_id=current_user.id,
            title=body.title or "",
            alias=body.alias or "",
            payload=payload,
        )
        db.add(row)
    # ``get_db`` commits on clean exit; flush is enough to populate the
    # autoincrement id + timestamps for the response below.
    await db.flush()
    return ToolDraftResponse(
        id=row.id,
        title=row.title or "",
        alias=row.alias or "",
        payload=dict(row.payload) if isinstance(row.payload, dict) else {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.delete("/admin/tool-drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool_draft(
    draft_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Discard one of the caller's drafts."""
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    row = await _fetch_owned_draft(db, draft_id=draft_id, owner_user_id=current_user.id)
    await db.delete(row)
    await db.flush()


@router.post("/admin/tools/{alias}/promote", response_model=AdminToolResponse)
async def promote_tool(
    alias: str,
    body: AdminPromoteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Move a tool through New → Test → Released. Requires ``tools.promote_state``."""
    await _require(db, current_user.id, Permission.TOOLS_PROMOTE_STATE)
    tool = await tool_service.promote_state(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        new_state=body.new_state,
    )
    return _tool_to_admin_response(tool)


@router.post("/admin/tools/{alias}/archive", response_model=AdminToolResponse)
async def archive_tool(
    alias: str,
    body: AdminArchiveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Soft-archive a tool. Requires ``tools.install``."""
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.archive(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        reason=body.reason or None,
    )
    return _tool_to_admin_response(tool)


@router.post("/admin/tools/{alias}/unarchive", response_model=AdminToolResponse)
async def unarchive_tool(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Re-activate a previously archived tool. Requires ``tools.install``.

    Idempotent: re-activating an already-active tool returns its
    current row unchanged.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.unarchive(
        db,
        actor_user_id=current_user.id,
        alias=alias,
    )
    return _tool_to_admin_response(tool)


@router.post(
    "/admin/tools",
    response_model=AdminToolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool(
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    metadata: Annotated[str, Form(description="JSON-serialised AdminToolMetadataPayload.")],
    descriptor_json: Annotated[
        UploadFile,
        File(description="Uploaded ``<alias>_descriptors.json`` with inputs / outputs / layout."),
    ],
    static_zip: Annotated[
        UploadFile,
        File(description="ZIP archive of the bundle's static files (scripts, data, etc.)."),
    ],
    icon: Annotated[
        UploadFile | None,
        File(description="Optional 100x100 PNG icon. Saved as ``<alias>.png``."),
    ] = None,
    presets: Annotated[
        UploadFile | None,
        File(description="Optional ``<alias>_presets.json`` preset-set file."),
    ] = None,
) -> AdminToolResponse:
    """Install a brand-new tool from a multipart upload. Requires ``tools.install``.

    The endpoint:

    1. Parses ``metadata`` (JSON-serialised :class:`AdminToolMetadataPayload`)
       and the uploaded descriptor JSON.
    2. Builds a complete :class:`ToolDescriptor` by merging form
       metadata (authoritative) with the uploaded JSON's inputs /
       outputs / groups / layout / retain_files / delete_io_files.
    3. Stages ``<bundles_dir>/<alias>/`` from the uploads — refuses if
       the alias is already in use on disk.
    4. Calls :meth:`ToolService.install`.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)

    descriptor_bytes = await _read_upload(
        descriptor_json,
        max_bytes=settings.tools.max_upload_bytes,
        label="descriptor JSON",
    )
    static_bytes = await _read_upload(
        static_zip,
        max_bytes=settings.tools.max_upload_bytes,
        label="static-files ZIP",
    )
    icon_bytes = await _read_upload(
        icon,
        max_bytes=settings.tools.max_upload_bytes,
        label="icon",
    )
    presets_bytes = await _read_upload(
        presets,
        max_bytes=settings.tools.max_upload_bytes,
        label="presets JSON",
    )
    if descriptor_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A descriptor JSON file is required.",
        )
    if static_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A static-files ZIP is required.",
        )

    descriptor = _build_descriptor_from_uploads(
        metadata_json=metadata, descriptor_bytes=descriptor_bytes
    )
    # Server forces the initial version on every install — operator's
    # uploaded descriptor JSON might say anything; we always start at
    # 0.1.0. Sync here so the per-version staging directory matches
    # what install() will eventually persist.
    descriptor = descriptor.model_copy(update={"version": INITIAL_VERSION})
    try:
        staged = stage_uploaded_bundle(
            bundles_dir=settings.tools.bundles_dir,
            descriptor=descriptor,
            static_zip_bytes=static_bytes,
            icon_bytes=icon_bytes,
            presets_bytes=presets_bytes,
            overwrite=False,
        )
    except BundleStagingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc

    tool = await tool_service.install(
        db,
        actor_user_id=current_user.id,
        descriptor=staged.descriptor,
        bundle_path=str(staged.bundle_path),
    )
    return _tool_to_admin_response(tool)


@router.post(
    "/admin/tools/{alias}/update-descriptor",
    response_model=AdminToolResponse,
)
async def update_tool_descriptor(
    alias: str,
    body: AdminToolDescriptorUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Edit a tool's descriptor metadata (no bundle change). Requires ``tools.install``.

    Reads the existing descriptor, applies the form overrides, and
    calls :meth:`ToolService.update_descriptor`. The bundle directory
    is left as-is — only the DB row's ``descriptor`` JSON changes.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=True
    )
    existing = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
    overrides = _descriptor_update_to_partial(body, alias=tool.alias)
    # ``main_script`` stays as-is (replace-bundle owns it).
    overrides["main_script"] = existing.get("main_script", overrides.get("main_script", ""))
    merged = {**existing, **overrides}
    try:
        descriptor = ToolDescriptor.model_validate(merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The edited descriptor failed validation: {exc}",
        ) from exc
    updated = await tool_service.update_descriptor(
        db,
        actor_user_id=current_user.id,
        descriptor=descriptor,
        expected_row_version=tool.row_version,
    )
    return _tool_to_admin_response(updated)


@router.post(
    "/admin/tools/{alias}/run-cap",
    response_model=AdminToolResponse,
)
async def set_tool_run_cap(
    alias: str,
    body: ToolRunCapRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Set or clear the per-tool parametric-study run cap.

    ``run_cap = None`` clears the override (study creation falls back
    to the install default). Row-level operational setting — no
    descriptor version bump. Requires ``tools.install`` (admin/SU).
    See ADR-0001 2026-05-20 revision /
    ``designs/parametric-studies.md`` §16.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    updated = await tool_service.set_run_cap(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        run_cap=body.run_cap,
    )
    return _tool_to_admin_response(updated)


@router.post(
    "/admin/tools/{alias}/python-env",
    response_model=AdminToolResponse,
)
async def set_tool_python_env(
    alias: str,
    body: ToolPythonEnvRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Pin (or clear) the Python env a tool's jobs run under.

    ``python_env_id = None`` clears the pin — jobs fall back to the
    MATOFX server's own interpreter (the "main MATOFX environment").
    Otherwise the id must reference an active ``python_env`` row.
    Row-level operational setting — no descriptor version bump.
    Requires ``tools.install`` (admin/SU).
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    updated = await tool_service.set_python_env(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        python_env_id=body.python_env_id,
    )
    return _tool_to_admin_response(updated)


def _bundle_relative_files(bundle: Path) -> list[AdminToolFileEntry]:
    """Walk ``bundle`` and emit one :class:`AdminToolFileEntry` per regular file.

    Used by the Files tab to populate its left-pane tree. ``is_text``
    is a best-effort guess (UTF-8 decode of the first 4 KB) — binary
    files still appear in the listing so the operator can see they
    exist, but the editor disables them on click.
    """
    if not bundle.is_dir():
        return []
    entries: list[AdminToolFileEntry] = []
    bundle_resolved = bundle.resolve()
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(bundle_resolved)
        except ValueError:  # pragma: no cover — symlinks shouldn't escape
            continue
        size = path.stat().st_size
        is_text = True
        try:
            with path.open("rb") as fh:
                fh.read(4096).decode("utf-8")
        except (UnicodeDecodeError, OSError):
            is_text = False
        entries.append(
            AdminToolFileEntry(
                relative_path=rel.as_posix(),
                size_bytes=size,
                is_text=is_text,
            )
        )
    return entries


@router.get(
    "/admin/tools/{alias}/files",
    response_model=AdminToolFileList,
)
async def list_tool_files(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolFileList:
    """List every file in the tool's current bundle directory.

    Drives the left pane of the Files tab on the admin Edit Tool page.
    Listing requires ``tools.install`` (same gate as edits) — the
    bundle's contents include scripts that might leak business logic,
    so a read on this endpoint is privileged.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=True
    )
    bundle = Path(str(tool.bundle_path))
    return AdminToolFileList(
        bundle_path=str(bundle),
        files=_bundle_relative_files(bundle),
    )


@router.get(
    "/admin/tools/{alias}/files/{file_path:path}",
    response_model=AdminToolFileContent,
)
async def read_tool_file(
    alias: str,
    file_path: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolFileContent:
    """Return the UTF-8 contents of a single file in the tool's current bundle.

    Path traversal, descriptor.json carve-out, and "must exist" checks
    all run inside :func:`_read_bundle_file_for_edit`; an
    :class:`InvalidFileEditError` from there maps to 400.
    """
    from matofx.tools.service import _read_bundle_file_for_edit  # noqa: PLC0415

    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=True
    )
    bundle = Path(str(tool.bundle_path))
    contents = _read_bundle_file_for_edit(bundle, file_path)
    return AdminToolFileContent(
        relative_path=file_path,
        contents=contents,
        size_bytes=len(contents.encode("utf-8")),
    )


@router.post(
    "/admin/tools/{alias}/files/{file_path:path}",
    response_model=AdminToolResponse,
)
async def save_tool_file(
    alias: str,
    file_path: str,
    body: AdminToolFileSaveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Save edited contents to a single bundle file. Minor version bump.

    Delegates to :meth:`ToolService.apply_file_edit`, which clones the
    current bundle to a fresh ``<bundles_dir>/<alias>/<new_version>/``
    and applies the edit there — the previous version's directory is
    left intact so jobs that captured the previous ``tool_version_id``
    keep resolving against their original files.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    updated = await tool_service.apply_file_edit(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        bundles_dir=settings.tools.bundles_dir,
        file_edits={file_path: body.contents},
        expected_row_version=body.expected_row_version,
    )
    return _tool_to_admin_response(updated)


@router.post(
    "/admin/tools/{alias}/set-approvers",
    response_model=AdminToolResponse,
)
async def set_tool_approvers(
    alias: str,
    body: AdminToolSetApprovers,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminToolResponse:
    """Bulk-replace a tool's approver list. Requires ``tools.install``.

    Used by the user-edit page's pre-flight when the operator's about
    to deactivate or remove the approver flag from someone who's the
    sole approver on one or more tools — the modal swaps the approver
    here *before* the deactivate / set-approver call would otherwise
    fail with :class:`SoleApproverError`.

    Implemented by fetching the descriptor, replacing only
    ``approver_usernames``, and calling
    :meth:`ToolService.update_descriptor` so the change goes through
    the same validation + audit pipeline as a metadata edit.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=False
    )
    existing = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
    merged = {**existing, "approver_usernames": list(body.approver_usernames)}
    try:
        descriptor = ToolDescriptor.model_validate(merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The edited approver list failed validation: {exc}",
        ) from exc
    updated = await tool_service.update_descriptor(
        db,
        actor_user_id=current_user.id,
        descriptor=descriptor,
        expected_row_version=tool.row_version,
    )
    return _tool_to_admin_response(updated)


@router.post(
    "/admin/tools/{alias}/replace-bundle",
    response_model=AdminToolResponse,
)
async def replace_tool_bundle(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    metadata: Annotated[str, Form(description="JSON-serialised AdminToolMetadataPayload.")],
    static_zip: Annotated[
        UploadFile,
        File(description="Fresh static-files ZIP."),
    ],
    descriptor_json: Annotated[
        UploadFile | None,
        File(
            description=(
                "Optional descriptor JSON (inputs / outputs / layout). Omit to "
                "keep the tool's current input/output settings and only swap the bundle."
            )
        ),
    ] = None,
    icon: Annotated[
        UploadFile | None,
        File(description="Optional new icon."),
    ] = None,
    presets: Annotated[
        UploadFile | None,
        File(description="Optional new presets file."),
    ] = None,
) -> AdminToolResponse:
    """Replace a tool's bundle on disk + descriptor. Requires ``tools.install``.

    The operator supplies a fresh static ZIP; the descriptor JSON is
    **optional** (as are icon / presets). Omitting the descriptor keeps
    the tool's current inputs / outputs / layout — useful when only the
    bundle's code changed. The endpoint major-bumps the version, stages
    the new bundle at
    ``<bundles_dir>/<alias>/<new_version>/`` (the previous version's
    directory is left untouched so older jobs keep resolving), and
    calls :meth:`ToolService.replace_bundle_and_descriptor` for the
    atomic descriptor + bundle swap with a single change-log snapshot.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    existing_tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=False
    )

    descriptor_bytes = await _read_upload(
        descriptor_json,
        max_bytes=settings.tools.max_upload_bytes,
        label="descriptor JSON",
    )
    static_bytes = await _read_upload(
        static_zip, max_bytes=settings.tools.max_upload_bytes, label="static-files ZIP"
    )
    icon_bytes = await _read_upload(icon, max_bytes=settings.tools.max_upload_bytes, label="icon")
    presets_bytes = await _read_upload(
        presets, max_bytes=settings.tools.max_upload_bytes, label="presets JSON"
    )
    if static_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Replace-bundle requires a static-files ZIP.",
        )

    # The descriptor JSON is optional. When it's omitted the operator is
    # only swapping the bundle's files (new code), so keep the tool's
    # current inputs / outputs / layout by reusing the existing descriptor.
    if descriptor_bytes is None:
        descriptor_bytes = json.dumps(existing_tool.descriptor or {}).encode("utf-8")

    descriptor = _build_descriptor_from_uploads(
        metadata_json=metadata, descriptor_bytes=descriptor_bytes
    )
    if descriptor.alias != alias:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"The uploaded bundle's alias ({descriptor.alias!r}) doesn't match "
                f"the URL alias ({alias!r}). Aliases are immutable."
            ),
        )

    # Pre-compute the new version so the staging directory matches what
    # the service will eventually persist. The service re-bumps from the
    # tool row's current version (same pure function, same answer) and
    # is the canonical source of truth — the endpoint just reads ahead
    # so it can place the bundle on disk first.
    new_version = bump_major(str(existing_tool.version))
    descriptor = descriptor.model_copy(update={"version": new_version})

    try:
        staged = stage_uploaded_bundle(
            bundles_dir=settings.tools.bundles_dir,
            descriptor=descriptor,
            static_zip_bytes=static_bytes,
            icon_bytes=icon_bytes,
            presets_bytes=presets_bytes,
            overwrite=False,
        )
    except BundleStagingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc

    refreshed = await tool_service.replace_bundle_and_descriptor(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        descriptor=staged.descriptor,
        bundle_path=str(staged.bundle_path),
        expected_row_version=existing_tool.row_version,
    )
    return _tool_to_admin_response(refreshed)


@router.post(
    "/admin/tools/{alias}/icon",
    response_model=AdminToolResponse,
)
async def update_tool_icon(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    icon: Annotated[
        UploadFile,
        File(description="Replacement PNG icon (100x100 recommended)."),
    ],
) -> AdminToolResponse:
    """Replace just a tool's icon, without re-uploading the bundle.

    Existing installs often skip the optional icon at create time;
    this endpoint lets the operator drop one in later (or swap an
    existing icon) without going through the full Replace bundle
    flow. The PNG is written in-place at ``<bundle_path>/<alias>.png``
    and the tool gets the usual patch-bump + change-log snapshot.

    Refuses non-PNG uploads (we accept any ``image/`` content type so
    Quasar's preview keeps working, but the file is named ``.png`` on
    disk regardless of MIME — operators uploading the wrong format
    get a broken icon they can immediately re-upload, not a system
    error).
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    existing_tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=False
    )
    icon_bytes = await _read_upload(icon, max_bytes=settings.tools.max_upload_bytes, label="icon")
    if not icon_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An icon file is required.",
        )
    refreshed = await tool_service.update_icon(
        db,
        actor_user_id=current_user.id,
        alias=alias,
        icon_bytes=icon_bytes,
        expected_row_version=existing_tool.row_version,
    )
    return _tool_to_admin_response(refreshed)


@router.post(
    "/admin/tools/{alias}/guide-images",
    response_model=AdminToolResponse,
)
async def update_tool_guide_images(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    manifest: Annotated[
        str,
        Form(
            description=(
                "JSON list, in display order, of {caption, existing_filename?}. "
                "An entry with no existing_filename consumes the next uploaded image."
            )
        ),
    ],
    images: Annotated[
        list[UploadFile] | None,
        File(description="New guide images (PNG/JPG/WebP), in the manifest's new-entry order."),
    ] = None,
) -> AdminToolResponse:
    """Replace a tool's static guide-image set in one call.

    The ``manifest`` carries the desired ordered list; each entry either
    keeps an existing image (by ``existing_filename``) or is a new upload
    (bytes come from ``images`` in order). Images are written into the
    bundle, orphans removed, and the descriptor's ``input_guides`` is
    rewritten with the usual patch-bump + change-log snapshot — mirrors
    the icon-replace flow. Requires ``tools.install``.
    """
    await _require(db, current_user.id, Permission.TOOLS_INSTALL)
    existing_tool = await tool_service.get(
        db, actor_user_id=current_user.id, alias=alias, include_archived=False
    )
    try:
        items = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The guide-image manifest wasn't valid JSON.",
        ) from exc
    if not isinstance(items, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The guide-image manifest must be a list.",
        )
    files_iter = iter(images or [])
    edits: list[GuideImageEdit] = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each guide-image entry must be an object.",
            )
        caption = str(item.get("caption") or "")
        existing = item.get("existing_filename")
        if existing:
            edits.append(GuideImageEdit(caption=caption, existing_filename=str(existing)))
            continue
        upload = next(files_iter, None)
        if upload is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A new guide image is missing its uploaded file.",
            )
        data = await _read_upload(
            upload, max_bytes=settings.tools.max_upload_bytes, label="guide image"
        )
        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A guide image upload was empty.",
            )
        ext = Path(upload.filename or "").suffix.lstrip(".").lower() or "png"
        edits.append(GuideImageEdit(caption=caption, new_bytes=data, new_ext=ext))
    try:
        refreshed = await tool_service.update_input_guides(
            db,
            actor_user_id=current_user.id,
            alias=alias,
            edits=edits,
            expected_row_version=existing_tool.row_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _tool_to_admin_response(refreshed)


def _tool_to_admin_response(tool: Tool) -> AdminToolResponse:
    short_description = ""
    long_description: str | None = None
    approver_usernames: list[str] = []
    teams: list[str] = []
    approvals_policy = "none"
    downstream_tools: list[str] = []
    if isinstance(tool.descriptor, dict):
        short_description, long_description = tool_descriptions(tool.descriptor)
        raw_approvers = tool.descriptor.get("approver_usernames")
        if isinstance(raw_approvers, list):
            approver_usernames = [str(u) for u in raw_approvers if isinstance(u, str)]
        raw_teams = tool.descriptor.get("teams")
        if isinstance(raw_teams, list):
            teams = [str(t) for t in raw_teams if isinstance(t, str)]
        raw_policy = tool.descriptor.get("approvals_policy")
        if isinstance(raw_policy, str):
            approvals_policy = raw_policy
        raw_downstream = tool.descriptor.get("downstream_tools")
        if isinstance(raw_downstream, list):
            downstream_tools = [str(t) for t in raw_downstream if isinstance(t, str)]
    return AdminToolResponse(
        alias=tool.alias,
        name=tool.name,
        version=tool.version,
        state=tool.state,
        short_description=short_description,
        long_description=long_description,
        archived_at=tool.archived_at,
        bundle_path=tool.bundle_path,
        approver_usernames=approver_usernames,
        teams=teams,
        approvals_policy=approvals_policy,
        downstream_tools=downstream_tools,
        teams_pending=bool(tool.teams_pending),
        is_surrogate=isinstance(tool.descriptor, dict)
        and tool.descriptor.get("surrogate_model_version_id") is not None,
    )


# Fields the form metadata payload OWNS — i.e. these come from the
# form, never from the uploaded JSON. Anything in the JSON for one
# of these keys is dropped before the merge.
_METADATA_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "alias",
        "name",
        "version",
        "short_description",
        "long_description",
        # Legacy single key — dropped from uploads so an old bundle whose
        # descriptor.json still carries ``description`` doesn't collide with
        # the form-supplied short/long (which always win on metadata).
        "description",
        "main_script",
        "teams",
        "downstream_tools",
        "resources",
        "licences_required",
        "duration_class",
        "approvals_policy",
        "approver_usernames",
        "email_notifications",
    }
)


def _metadata_to_descriptor_dict(payload: AdminToolMetadataPayload) -> dict[str, Any]:
    """Spread the form-side metadata into a partial ToolDescriptor dict.

    Used both for fresh installs (merged with the uploaded JSON) and
    for metadata-only edits (merged with the existing descriptor).
    """
    resources: dict[str, Any] = {
        "cpu_cores": payload.cpu_cores,
        "ram_mb": payload.ram_mb,
        "gpu_memory_mb": payload.gpu_memory_mb,
    }
    licences = [
        LicenceRequirement(name=str(item["name"]), count=int(item.get("count", 1))).model_dump(
            mode="json"
        )
        for item in payload.licences_required
    ]
    return {
        "alias": payload.alias,
        "name": payload.name,
        "version": payload.version,
        "short_description": payload.short_description,
        "long_description": payload.long_description,
        "main_script": payload.main_script,
        "teams": list(payload.teams),
        "downstream_tools": list(payload.downstream_tools),
        "resources": resources,
        "licences_required": licences,
        "duration_class": payload.duration_class,
        "approvals_policy": payload.approvals_policy,
        "approver_usernames": list(payload.approver_usernames),
        "email_notifications": payload.email_notifications,
    }


def _descriptor_update_to_partial(body: AdminToolDescriptorUpdate, alias: str) -> dict[str, Any]:
    """Spread the metadata-edit body into a partial ToolDescriptor dict.

    Same fields as :func:`_metadata_to_descriptor_dict` but takes the
    ``alias`` from the URL (immutable on edit) and skips ``main_script``
    (changing the entry point belongs to the replace-bundle flow).
    """
    resources: dict[str, Any] = {
        "cpu_cores": body.cpu_cores,
        "ram_mb": body.ram_mb,
        "gpu_memory_mb": body.gpu_memory_mb,
    }
    licences = [
        LicenceRequirement(name=str(item["name"]), count=int(item.get("count", 1))).model_dump(
            mode="json"
        )
        for item in body.licences_required
    ]
    out: dict[str, Any] = {
        "alias": alias,
        "name": body.name,
        "version": body.version,
        "short_description": body.short_description,
        "long_description": body.long_description,
        "teams": list(body.teams),
        "downstream_tools": list(body.downstream_tools),
        "resources": resources,
        "licences_required": licences,
        "duration_class": body.duration_class,
        "approvals_policy": body.approvals_policy,
        "approver_usernames": list(body.approver_usernames),
        "email_notifications": body.email_notifications,
    }
    # Wizard-supplied descriptor-body fields. Each is optional —
    # absent means "leave the existing descriptor's field alone";
    # present means "overlay this onto the merge". The merge happens
    # in :func:`update_tool_descriptor` via ``{**existing, **out}``.
    list_fields = (
        "inputs",
        "input_groups",
        "outputs",
        "output_groups",
        "retain_files",
        "delete_io_files",
        "dependencies",
        "cross_field_constraints",
        "presets",
    )
    dict_fields = ("input_column_layout", "output_column_layout", "execution_backend")
    scalar_fields = ("input_groups_start_collapsed", "require_all_groups_opened")
    for name in list_fields:
        value = getattr(body, name)
        if value is not None:
            out[name] = list(value)
    for name in dict_fields:
        value = getattr(body, name)
        if value is not None:
            out[name] = dict(value)
    for name in scalar_fields:
        value = getattr(body, name)
        if value is not None:
            out[name] = value
    return out


def _build_descriptor_from_uploads(
    *,
    metadata_json: str,
    descriptor_bytes: bytes,
) -> ToolDescriptor:
    """Combine the form metadata payload + uploaded descriptor JSON.

    The form's :class:`AdminToolMetadataPayload` owns the metadata
    fields (alias, name, version, description, teams, resources, etc.).
    The uploaded JSON owns the inputs / outputs / groups / column-layout
    / retain_files / delete_io_files sections — everything that
    describes the tool's run-time contract rather than its identity.

    Raises:
        HTTPException 400: malformed metadata JSON, malformed
            descriptor JSON, or the resulting merged descriptor fails
            Pydantic validation.
    """
    try:
        payload = AdminToolMetadataPayload.model_validate_json(metadata_json)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The tool metadata block is malformed: {exc}",
        ) from exc

    try:
        descriptor_dict = json.loads(descriptor_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The descriptor JSON file isn't valid JSON: {exc}",
        ) from exc
    if not isinstance(descriptor_dict, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The descriptor JSON file must decode to a JSON object.",
        )

    # Drop fields the form owns from the uploaded JSON so the form
    # always wins on metadata.
    merged = {k: v for k, v in descriptor_dict.items() if k not in _METADATA_OWNED_FIELDS}
    merged.update(_metadata_to_descriptor_dict(payload))

    try:
        return ToolDescriptor.model_validate(merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The combined tool descriptor failed validation: {exc}",
        ) from exc


async def _read_upload(upload: UploadFile | None, *, max_bytes: int, label: str) -> bytes | None:
    """Read an UploadFile fully and enforce the per-file size cap.

    ``max_bytes`` is enforced once everything is in memory; the
    multipart parser already streams the body so we don't double-buffer.
    Empty uploads (the operator left the field blank) come through as
    a zero-byte read on a ``filename`` of ``""`` — those are treated
    as "no file" and ``None`` is returned.
    """
    if upload is None or not upload.filename:
        return None
    return await read_upload_capped(upload, max_bytes=max_bytes, what=label)


# ---------- storage_locations --------------------------------------------


@router.get(
    "/admin/storage-locations",
    response_model=list[AdminStorageLocationResponse],
)
async def list_storage_locations(
    db: Annotated[AsyncSession, Depends(get_db)],
    storage_service: Annotated[StorageLocationsService, Depends(get_storage_locations_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminStorageLocationResponse]:
    """List every storage location. Requires ``storage_locations.manage``."""
    await _require(db, current_user.id, Permission.STORAGE_LOCATIONS_MANAGE)
    rows = await storage_service.list_all(db)
    return [_storage_to_response(r) for r in rows]


@router.post(
    "/admin/storage-locations",
    response_model=AdminStorageLocationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_storage_location(
    body: AdminStorageLocationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage_service: Annotated[StorageLocationsService, Depends(get_storage_locations_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminStorageLocationResponse:
    """Register a new storage location."""
    row = await storage_service.create(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        mount_path=body.mount_path,
    )
    return _storage_to_response(row)


@router.post(
    "/admin/storage-locations/{row_id}/retire",
    response_model=AdminStorageLocationResponse,
)
async def retire_storage_location(
    row_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage_service: Annotated[StorageLocationsService, Depends(get_storage_locations_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminStorageLocationResponse:
    """Retire a storage location (no hard delete; old jobs keep resolving)."""
    row = await storage_service.retire(db, actor_user_id=current_user.id, row_id=row_id)
    return _storage_to_response(row)


def _storage_to_response(
    row: object,
) -> AdminStorageLocationResponse:
    return AdminStorageLocationResponse(
        id=row.id,  # type: ignore[attr-defined]
        name=row.name,  # type: ignore[attr-defined]
        mount_path=row.mount_path,  # type: ignore[attr-defined]
        is_active=row.is_active,  # type: ignore[attr-defined]
        retired_at=row.retired_at,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


# ---------- python_envs --------------------------------------------------


@router.get("/admin/python-envs", response_model=list[AdminPythonEnvResponse])
async def list_python_envs(
    db: Annotated[AsyncSession, Depends(get_db)],
    python_envs: Annotated[PythonEnvsService, Depends(get_python_envs_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminPythonEnvResponse]:
    """List every Python env. Requires ``python_envs.manage``."""
    await _require(db, current_user.id, Permission.PYTHON_ENVS_MANAGE)
    rows = await python_envs.list_all(db)
    return [_python_env_to_response(r) for r in rows]


@router.post(
    "/admin/python-envs",
    response_model=AdminPythonEnvResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_python_env(
    body: AdminPythonEnvCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    python_envs: Annotated[PythonEnvsService, Depends(get_python_envs_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminPythonEnvResponse:
    """Register a new Python env."""
    row = await python_envs.create(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        interpreter_path=body.interpreter_path,
        storage_location_id=body.storage_location_id,
    )
    return _python_env_to_response(row)


@router.post(
    "/admin/python-envs/{row_id}/deactivate",
    response_model=AdminPythonEnvResponse,
)
async def deactivate_python_env(
    row_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    python_envs: Annotated[PythonEnvsService, Depends(get_python_envs_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminPythonEnvResponse:
    """Mark a Python env inactive."""
    row = await python_envs.deactivate(db, actor_user_id=current_user.id, row_id=row_id)
    return _python_env_to_response(row)


# ---------- aos_hosts ----------------------------------------------------


async def _secret_present(store: SecretsStore, secret_ref: str) -> bool:
    """Whether ``secret_ref`` resolves in the store (without revealing it).

    A token that exists but can't be decrypted (wrong key / tampered) is
    still reported as *present* — the admin needs to know the ref is set so
    they can fix the key rather than re-enter it blindly.
    """
    try:
        await store.resolve(secret_ref)
    except SecretNotFoundError:
        return False
    except SecretsError:
        return True
    return True


@router.get("/admin/aos-hosts", response_model=list[AdminAosHostResponse])
async def list_aos_hosts(
    db: Annotated[AsyncSession, Depends(get_db)],
    aos_hosts: Annotated[AosHostsService, Depends(get_aos_hosts_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminAosHostResponse]:
    """List every always-on server. Requires ``aos_hosts.manage``."""
    await _require(db, current_user.id, Permission.AOS_HOSTS_MANAGE)
    rows = await aos_hosts.list_all(db)
    return [_aos_host_to_response(r, await _secret_present(store, r.secret_ref)) for r in rows]


@router.post(
    "/admin/aos-hosts",
    response_model=AdminAosHostResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_aos_host(
    body: AdminAosHostCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    aos_hosts: Annotated[AosHostsService, Depends(get_aos_hosts_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminAosHostResponse:
    """Register a new always-on server."""
    row = await aos_hosts.create(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        hostname=body.hostname,
        os=body.os,
        username=body.username,
        secret_ref=body.secret_ref,
        port=body.port,
        max_concurrency=body.max_concurrency,
        cpu_cores=body.cpu_cores,
        memory_mb=body.memory_mb,
    )
    return _aos_host_to_response(row, await _secret_present(store, row.secret_ref))


@router.put("/admin/aos-hosts/{row_id}", response_model=AdminAosHostResponse)
async def update_aos_host(
    row_id: int,
    body: AdminAosHostUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    aos_hosts: Annotated[AosHostsService, Depends(get_aos_hosts_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminAosHostResponse:
    """Update an always-on server's editable fields (name is immutable)."""
    row = await aos_hosts.update(
        db,
        actor_user_id=current_user.id,
        row_id=row_id,
        hostname=body.hostname,
        os=body.os,
        username=body.username,
        secret_ref=body.secret_ref,
        port=body.port,
        max_concurrency=body.max_concurrency,
        cpu_cores=body.cpu_cores,
        memory_mb=body.memory_mb,
    )
    return _aos_host_to_response(row, await _secret_present(store, row.secret_ref))


@router.post("/admin/aos-hosts/{row_id}/deactivate", response_model=AdminAosHostResponse)
async def deactivate_aos_host(
    row_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    aos_hosts: Annotated[AosHostsService, Depends(get_aos_hosts_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminAosHostResponse:
    """Deactivate an always-on server (accepts no new jobs)."""
    row = await aos_hosts.set_active(
        db, actor_user_id=current_user.id, row_id=row_id, is_active=False
    )
    return _aos_host_to_response(row, await _secret_present(store, row.secret_ref))


@router.post("/admin/aos-hosts/{row_id}/activate", response_model=AdminAosHostResponse)
async def activate_aos_host(
    row_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    aos_hosts: Annotated[AosHostsService, Depends(get_aos_hosts_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminAosHostResponse:
    """Re-activate a deactivated always-on server."""
    row = await aos_hosts.set_active(
        db, actor_user_id=current_user.id, row_id=row_id, is_active=True
    )
    return _aos_host_to_response(row, await _secret_present(store, row.secret_ref))


def _aos_host_to_response(row: AosHost, secret_present: bool) -> AdminAosHostResponse:
    return AdminAosHostResponse(
        id=row.id,
        name=row.name,
        hostname=row.hostname,
        os=row.os,
        transport=row.transport,
        port=row.port,
        username=row.username,
        secret_ref=row.secret_ref,
        is_active=row.is_active,
        max_concurrency=row.max_concurrency,
        cpu_cores=row.cpu_cores,
        memory_mb=row.memory_mb,
        secret_present=secret_present,
        created_at=row.created_at,
    )


# ---------- hpc_clusters -------------------------------------------------


@router.get("/admin/hpc-clusters", response_model=list[AdminHpcClusterResponse])
async def list_hpc_clusters(
    db: Annotated[AsyncSession, Depends(get_db)],
    hpc_clusters: Annotated[HpcClustersService, Depends(get_hpc_clusters_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminHpcClusterResponse]:
    """List every HPC cluster. Requires ``hpc_clusters.manage``."""
    await _require(db, current_user.id, Permission.HPC_CLUSTERS_MANAGE)
    rows = await hpc_clusters.list_all(db)
    return [_hpc_cluster_to_response(r, await _secret_present(store, r.secret_ref)) for r in rows]


@router.post(
    "/admin/hpc-clusters",
    response_model=AdminHpcClusterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_hpc_cluster(
    body: AdminHpcClusterCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    hpc_clusters: Annotated[HpcClustersService, Depends(get_hpc_clusters_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminHpcClusterResponse:
    """Register a new HPC cluster."""
    row = await hpc_clusters.create(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        head_node=body.head_node,
        scheduler=body.scheduler,
        username=body.username,
        secret_ref=body.secret_ref,
        port=body.port,
        default_queue=body.default_queue,
        account=body.account,
        wckey=body.wckey,
    )
    return _hpc_cluster_to_response(row, await _secret_present(store, row.secret_ref))


@router.put("/admin/hpc-clusters/{row_id}", response_model=AdminHpcClusterResponse)
async def update_hpc_cluster(
    row_id: int,
    body: AdminHpcClusterUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    hpc_clusters: Annotated[HpcClustersService, Depends(get_hpc_clusters_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminHpcClusterResponse:
    """Update an HPC cluster's editable fields (name is immutable)."""
    row = await hpc_clusters.update(
        db,
        actor_user_id=current_user.id,
        row_id=row_id,
        head_node=body.head_node,
        scheduler=body.scheduler,
        username=body.username,
        secret_ref=body.secret_ref,
        port=body.port,
        default_queue=body.default_queue,
        account=body.account,
        wckey=body.wckey,
    )
    return _hpc_cluster_to_response(row, await _secret_present(store, row.secret_ref))


@router.post("/admin/hpc-clusters/{row_id}/deactivate", response_model=AdminHpcClusterResponse)
async def deactivate_hpc_cluster(
    row_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    hpc_clusters: Annotated[HpcClustersService, Depends(get_hpc_clusters_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminHpcClusterResponse:
    """Deactivate an HPC cluster (accepts no new jobs)."""
    row = await hpc_clusters.set_active(
        db, actor_user_id=current_user.id, row_id=row_id, is_active=False
    )
    return _hpc_cluster_to_response(row, await _secret_present(store, row.secret_ref))


@router.post("/admin/hpc-clusters/{row_id}/activate", response_model=AdminHpcClusterResponse)
async def activate_hpc_cluster(
    row_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    hpc_clusters: Annotated[HpcClustersService, Depends(get_hpc_clusters_service)],
    store: Annotated[SecretsStore, Depends(get_secrets_store)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminHpcClusterResponse:
    """Re-activate a deactivated HPC cluster."""
    row = await hpc_clusters.set_active(
        db, actor_user_id=current_user.id, row_id=row_id, is_active=True
    )
    return _hpc_cluster_to_response(row, await _secret_present(store, row.secret_ref))


def _hpc_cluster_to_response(row: HpcCluster, secret_present: bool) -> AdminHpcClusterResponse:
    return AdminHpcClusterResponse(
        id=row.id,
        name=row.name,
        head_node=row.head_node,
        scheduler=row.scheduler,
        transport=row.transport,
        port=row.port,
        username=row.username,
        secret_ref=row.secret_ref,
        default_queue=row.default_queue,
        account=row.account,
        wckey=row.wckey,
        is_active=row.is_active,
        secret_present=secret_present,
        created_at=row.created_at,
    )


def _python_env_to_response(row: object) -> AdminPythonEnvResponse:
    return AdminPythonEnvResponse(
        id=row.id,  # type: ignore[attr-defined]
        name=row.name,  # type: ignore[attr-defined]
        interpreter_path=row.interpreter_path,  # type: ignore[attr-defined]
        storage_location_id=row.storage_location_id,  # type: ignore[attr-defined]
        is_active=row.is_active,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


# ---------- runtime settings (wraps existing service) --------------------


@router.get("/admin/runtime-settings", response_model=AdminRuntimeSettingsResponse)
async def get_runtime_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings_service: Annotated[RuntimeSettingsService, Depends(get_runtime_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRuntimeSettingsResponse:
    """Snapshot every admin-tunable runner setting. Requires ``runtime_settings.manage``."""
    snapshot = await settings_service.get_all(db, actor_user_id=current_user.id)
    return AdminRuntimeSettingsResponse(settings=snapshot)


@router.post(
    "/admin/runtime-settings/{key}",
    response_model=AdminRuntimeSettingsResponse,
)
async def set_runtime_setting(
    key: str,
    body: AdminRuntimeSettingValue,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings_service: Annotated[RuntimeSettingsService, Depends(get_runtime_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRuntimeSettingsResponse:
    """Set one runner setting key. Returns the full updated snapshot."""
    await settings_service.set(db, actor_user_id=current_user.id, key=key, value=body.value)
    snapshot = await settings_service.get_all(db, actor_user_id=current_user.id)
    return AdminRuntimeSettingsResponse(settings=snapshot)


@router.post(
    "/admin/runtime-settings/{key}/reset",
    response_model=AdminRuntimeSettingsResponse,
)
async def reset_runtime_setting(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings_service: Annotated[RuntimeSettingsService, Depends(get_runtime_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminRuntimeSettingsResponse:
    """Reset one runner setting key to its catalog default."""
    await settings_service.reset(db, actor_user_id=current_user.id, key=key)
    snapshot = await settings_service.get_all(db, actor_user_id=current_user.id)
    return AdminRuntimeSettingsResponse(settings=snapshot)


# ---------- active sessions (ADR-0019 slice 4) ----------------------------
#
# The break-glass surface. A Super User admitted over the licence cap
# needs somewhere to go and free a real place; everyone administering a
# CSL install needs to see who is holding one. Gated on ``users.manage``
# — the same right that already covers enabling and disabling accounts,
# which is the closest existing equivalent to "sign someone out".


@router.get("/admin/sessions", response_model=AdminActiveSessionsResponse)
async def list_active_sessions(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdminActiveSessionsResponse:
    """Live sessions, newest first, with the licence capacity. ``users.manage``."""
    from matofx.licence import resolve_licence  # noqa: PLC0415
    from matofx.licence.models import CSL  # noqa: PLC0415
    from matofx.licence.slots import count_live_sessions  # noqa: PLC0415
    from matofx.users.settings_overlay import effective_session_policy  # noqa: PLC0415

    await _require(db, current_user.id, Permission.USERS_MANAGE)

    now = _dt.datetime.now(tz=_dt.UTC)
    policy = await effective_session_policy(db, settings.auth)
    idle_cutoff = now - _dt.timedelta(minutes=policy.idle_timeout_minutes)
    hard_cutoff = now - _dt.timedelta(hours=policy.hard_timeout_hours)

    stmt = (
        sa.select(AuthSession, User, Role.name)
        .join(User, User.id == AuthSession.user_id)
        .outerjoin(Role, Role.id == User.role_id)
        .where(
            AuthSession.revoked_at.is_(None),
            AuthSession.last_activity_at >= idle_cutoff,
            AuthSession.created_at >= hard_cutoff,
        )
        .order_by(AuthSession.last_activity_at.desc())
    )
    current_token_id = getattr(request.state, "session_id", None)
    rows: list[AdminActiveSessionRow] = []
    for auth_session, user, role_name in (await db.execute(stmt)).all():
        last_activity = _aware(auth_session.last_activity_at)
        rows.append(
            AdminActiveSessionRow(
                id=auth_session.id,
                user_id=user.id,
                username=user.username,
                full_name=user.full_name or user.username,
                role=role_name or "",
                ip_address=auth_session.ip_address,
                started_at=_aware(auth_session.created_at),
                last_activity_at=last_activity,
                idle_minutes=max(0, int((now - last_activity).total_seconds() // 60)),
                is_you=auth_session.id == current_token_id,
                breakglass=bool(getattr(auth_session, "breakglass", False)),
            )
        )

    resolution = resolve_licence(settings)
    manifest = resolution.status.manifest if resolution.status is not None else None
    metered = manifest is not None and manifest.licence_type == CSL and not resolution.read_only
    in_use = await count_live_sessions(db, settings=settings, now=now)
    return AdminActiveSessionsResponse(
        sessions=rows,
        licence_type=manifest.licence_type if manifest else None,
        n_slots=manifest.n_slots if (manifest and metered) else None,
        in_use=in_use,
        over_cap=bool(metered and manifest and in_use > manifest.n_slots),
        metered=metered,
    )


@router.post("/admin/sessions/{session_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_active_session(
    request: Request,
    session_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Sign a user out, freeing their licence place. ``users.manage``.

    Refuses to revoke the caller's own session: an admin who signs
    themselves out mid-rescue has made the problem worse, and the UI
    hides the button for that row anyway.
    """
    from matofx.licence import resolve_licence  # noqa: PLC0415
    from matofx.licence.slots import release_slot  # noqa: PLC0415

    await _require(db, current_user.id, Permission.USERS_MANAGE)

    row = await db.get(AuthSession, session_id)
    if row is None or row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That session has already ended.",
        )
    if row.id == getattr(request.state, "session_id", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That's the session you're using — sign out normally instead.",
        )

    row.revoked_at = _dt.datetime.now(tz=_dt.UTC)
    await db.flush()
    audit = request.app.state.audit
    await audit.record(
        db,
        RecordInput(
            actor=f"user:{current_user.id}",
            actor_type="user",
            action="auth.session.revoke",
            resource_type="session",
            resource_id=str(row.id),
            outcome="success",
            reason="signed out by an administrator",
        ),
    )
    await release_slot(
        db,
        audit=audit,
        resolution=resolve_licence(settings),
        user_id=row.user_id,
        session_id=row.id,
        actor=f"user:{current_user.id}",
        reason="signed out by an administrator",
    )


def _aware(value: _dt.datetime) -> _dt.datetime:
    """Treat naive timestamps (SQLite round-trip) as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=_dt.UTC)


@router.get("/admin/licence/capacity", response_model=AdminLicenceCapacityResponse)
async def get_licence_capacity(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdminLicenceCapacityResponse:
    """Floating-assignment capacity, for the Users page. ``users.manage``.

    Lets an administrator see how many places are left *before* they
    fill in a new-user form, rather than discovering the cap when the
    save is refused (ADR-0019 slice 5).
    """
    from matofx.licence import count_assignments, resolve_licence  # noqa: PLC0415
    from matofx.licence.models import FAL  # noqa: PLC0415

    await _require(db, current_user.id, Permission.USERS_MANAGE)
    resolution = resolve_licence(settings)
    manifest = resolution.status.manifest if resolution.status is not None else None
    metered = manifest is not None and manifest.licence_type == FAL and not resolution.read_only
    in_use = await count_assignments(db)
    return AdminLicenceCapacityResponse(
        metered=metered,
        licence_type=manifest.licence_type if manifest else None,
        n_slots=manifest.n_slots if (manifest and metered) else None,
        in_use=in_use,
        remaining=max(0, manifest.n_slots - in_use) if (manifest and metered) else None,
    )


_log = logging.getLogger(__name__)


# ---------- read-only mode + chain integrity (ADR-0019 slice 6) -----------


@router.get("/site/mode", response_model=SiteModeResponse)
async def get_site_mode(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SiteModeResponse:
    """Whether the site is writable, and why not. Any signed-in user.

    Deliberately **not** gated on an admin permission: every user needs
    to know the site is read-only — that is the difference between "the
    button did nothing" and "the site is in a known state".
    """
    from matofx.api.readonly import _foreign_install, _licence_enforced  # noqa: PLC0415
    from matofx.compliance.integrity import current_status  # noqa: PLC0415
    from matofx.licence import resolve_licence  # noqa: PLC0415

    resolution = resolve_licence(settings)
    compliance = await _compliance_reminder(db, resolution, current_user)
    integrity = await current_status(db)
    role = await _role_name_for(db, current_user)
    # Same exemption the middleware applies, or a never-licensed dev
    # install would *report* read-only while writes sailed through.
    licence_read_only = resolution.read_only and _licence_enforced(settings)

    # A licence issued for another install blocks writes in the
    # middleware; the banner has to say so too, or a user hits a 503 the
    # page cannot explain.
    foreign = _foreign_install(request, resolution) if _licence_enforced(settings) else None
    reason: str | None
    banner: str | None
    if foreign is not None:
        reason, banner = foreign.code, foreign.message
        licence_read_only = True
    elif licence_read_only:
        # Same split the middleware makes, so the banner and the refusal
        # a user just hit never disagree about why.
        reason = "licence_invalid" if resolution.fail_closed else "licence"
        banner = resolution.banner
    elif integrity.read_only:
        reason, banner = "integrity", integrity.banner
    else:
        reason, banner = None, integrity.banner  # acknowledged failures still warn

    return SiteModeResponse(
        read_only=bool(licence_read_only or integrity.read_only),
        reason=reason,
        banner=banner,
        licence_state=resolution.status.state if resolution.status else None,
        integrity_ok=integrity.ok,
        integrity_acknowledged=integrity.acknowledged,
        can_acknowledge=(not integrity.ok and not integrity.acknowledged and role == "super_user"),
        compliance_due=compliance.due,
        compliance_banner=compliance.banner,
        compliance_last_generated_at=compliance.last_generated_at,
        compliance_due_at=compliance.due_at,
    )


@dataclass(frozen=True)
class _ComplianceReminder:
    """What the chrome needs to draw (or not draw) the reminder banner."""

    due: bool = False
    banner: str | None = None
    last_generated_at: _dt.datetime | None = None
    due_at: _dt.datetime | None = None
    cadence_months: int = 0


async def _compliance_reminder(
    db: AsyncSession, resolution: Any, current_user: User
) -> _ComplianceReminder:
    """Is a compliance report owed, and is this user the one to produce it?

    Two gates, both deliberate:

    - **Permission.** Only ``audit.read`` — the right the compliance page
      itself is gated on — sees the banner. Telling a user a report is
      overdue when the page to generate it is closed to them is a dead
      end, not a prompt.
    - **Cadence from the licence.** How often a customer reports is a
      term of the agreement, so it rides on the signed manifest rather
      than sitting in site settings where it could be edited away. No
      readable licence means no cadence and no banner.

    Never raises: a failure here must not stop ``/site/mode`` answering,
    because the read-only banner it also carries is the more important
    of the two.
    """
    from matofx.compliance.schedule import banner_text, reporting_status  # noqa: PLC0415

    try:
        if not await user_has_permission(db, current_user.id, Permission.AUDIT_READ):
            return _ComplianceReminder()
        manifest = resolution.status.manifest if resolution.status is not None else None
        if manifest is None:
            return _ComplianceReminder()
        status_ = await reporting_status(
            db,
            cadence_months=manifest.compliance_report_months,
            now=_dt.datetime.now(_dt.UTC),
        )
        return _ComplianceReminder(
            due=status_.due,
            banner=banner_text(status_),
            last_generated_at=status_.last_generated_at,
            due_at=status_.due_at,
            cadence_months=status_.cadence_months,
        )
    except Exception:  # pragma: no cover - defensive
        _log.exception("compliance reminder check failed")
        return _ComplianceReminder()


@router.post("/admin/integrity/acknowledge", response_model=SiteModeResponse)
async def acknowledge_integrity_failure(
    body: IntegrityAcknowledgeRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SiteModeResponse:
    """Lift read-only after a failed integrity check (Super User only).

    Not a permission check but a *role* check: this is the one action
    that sets aside the tamper-evidence guarantee, so it is restricted
    to the tier that can't be granted by configuration.
    """
    from matofx.compliance.integrity import acknowledge  # noqa: PLC0415

    role = await _role_name_for(db, current_user)
    if role != "super_user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a Super User can acknowledge an integrity failure.",
        )
    await acknowledge(
        db,
        audit=request.app.state.audit,
        actor=f"user:{current_user.id}",
        note=body.note,
    )
    return await get_site_mode(request, db, current_user, settings)


async def _role_name_for(db: AsyncSession, user: User) -> str:
    """The user's base-role name, or ``""``."""
    if user.role_id is None:
        return ""
    role = await db.get(Role, user.role_id)
    return role.name if role is not None else ""


# ---------- compliance reports (ADR-0019 §B7) -----------------------------
#
# The ADR specified an "Admin → Licence & compliance page button" from the
# start and deferred it to slice 6's UI work, where it was never built — so
# for two months the artefact a customer's supplier asks for could only be
# produced by an administrator with shell access on the server. That is a
# gap the customer feels, not an internal one.
#
# Gated on ``audit.read``: the permission is already documented as the one a
# "compliance-focused role" holds, and its holder can read the entire audit
# log, which is strictly more sensitive than a report of aggregates and
# licence events.


def _report_row(report: Any) -> ComplianceReportRow:
    """Map an archived ``ComplianceReport`` row to its wire shape."""
    return ComplianceReportRow(
        counter=int(report.counter),
        generated_at=_iso_or_empty(report.generated_at),
        period_from=_iso_or_empty(report.period_from),
        period_to=_iso_or_empty(report.period_to),
        chain_ok=bool(report.chain_ok),
        licence_type=report.licence_type,
        content_sha256=str(report.content_sha256),
        generated_by=str(report.generated_by),
        tree_size=int(report.tree_size or 0),
    )


def _iso_or_empty(value: Any) -> str:
    """ISO-8601 for a datetime column, or an empty string when absent."""
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _load_report_identity(settings: Settings) -> Any:
    """The report signing identity — licence manifest first, file second.

    Deliberately the same precedence as the CLI's ``_load_issued_identity``:
    two orders would mean the page and the command could sign with
    different keys, and the vendor's ledger pins one report key per
    install.
    """
    from matofx.compliance import load_identity_file  # noqa: PLC0415
    from matofx.compliance.errors import InstallIdentityError  # noqa: PLC0415
    from matofx.licence import report_identity, resolve_licence  # noqa: PLC0415
    from matofx.licence.loader import issuer_required  # noqa: PLC0415

    from_licence = report_identity(resolve_licence(settings))
    if from_licence is not None:
        return from_licence

    identity_file = settings.compliance.identity_file
    if identity_file is None:
        raise InstallIdentityError(
            "No report signing key is available: the installed licence carries none, "
            "and no install-identity file is configured. Install the licence your "
            "supplier issued for this install, then try again."
        )
    return load_identity_file(
        Path(identity_file),
        extra_issuer_keys=dict(settings.compliance.issuer_public_keys),
        require_issuer=issuer_required(settings.environment),
    )


@router.get("/admin/compliance/reports", response_model=ComplianceReportListResponse)
async def list_compliance_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ComplianceReportListResponse:
    """Every report this install has generated, newest first. ``audit.read``."""
    from matofx.compliance.errors import ComplianceError  # noqa: PLC0415
    from matofx.compliance.orm import ComplianceReport, InstallIdentity  # noqa: PLC0415
    from matofx.licence import resolve_licence  # noqa: PLC0415
    from matofx.licence.errors import LicenceError  # noqa: PLC0415

    await _require(db, current_user.id, Permission.AUDIT_READ)

    rows = (
        (await db.execute(sa.select(ComplianceReport).order_by(ComplianceReport.counter.desc())))
        .scalars()
        .all()
    )
    identity = await db.get(InstallIdentity, 1)

    # Resolve the signing key now, so the page can say *why* it cannot
    # generate instead of offering a button that always fails.
    can_generate, blocked = True, None
    try:
        _load_report_identity(settings)
    except (ComplianceError, LicenceError) as exc:
        can_generate, blocked = False, str(exc)

    # Same helper the chrome banner is drawn from, so the page and the
    # banner cannot disagree about whether a report is owed.
    schedule = await _compliance_reminder(db, resolve_licence(settings), current_user)

    return ComplianceReportListResponse(
        reports=[_report_row(row) for row in rows],
        install_id=identity.install_id if identity else None,
        customer=identity.customer if identity else None,
        can_generate=can_generate,
        blocked_reason=blocked,
        reporting_due=schedule.due,
        reporting_due_at=schedule.due_at,
        reporting_cadence_months=schedule.cadence_months,
    )


@router.post(
    "/admin/compliance/reports",
    response_model=ComplianceReportGeneratedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_compliance_report(
    body: ComplianceReportGenerateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ComplianceReportGeneratedResponse:
    """Generate, sign and archive a report. ``audit.read``.

    **Never fails closed** (ADR §B9). A failing chain check, a broken
    licence or a detected breach must still produce a report saying so —
    refusing at exactly the moment something looks wrong would destroy the
    evidence needed to explain it. So a failed verification comes back as
    ``warning`` on a 201, not as an error status.
    """
    from matofx.compliance import generate_report  # noqa: PLC0415
    from matofx.compliance.errors import ComplianceError  # noqa: PLC0415
    from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415
    from matofx.licence import licence_snapshot, resolve_licence  # noqa: PLC0415
    from matofx.licence.errors import LicenceError  # noqa: PLC0415

    await _require(db, current_user.id, Permission.AUDIT_READ)

    period_from = None
    if body.since:
        try:
            period_from = _dt.datetime.fromisoformat(body.since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Couldn't read {body.since!r} as a date. Use YYYY-MM-DD.",
            ) from exc

    try:
        issued = _load_report_identity(settings)
        # Make sure this install has named itself. Since §16.57 the
        # identity is minted locally at first boot and `ensure_…` is
        # idempotent — but the only startup path that calls it is licence
        # binding, which returns early when no licence is configured. An
        # install being commissioned *before* its licence arrives would
        # otherwise be told to run `matofx compliance adopt`, the command
        # that inversion superseded. There is always a local identity;
        # this is what makes that true here too.
        await ensure_install_identity(db)
        generated = await generate_report(
            db,
            settings=settings,
            issued=issued,
            actor=f"user:{current_user.id}",
            period_from=period_from,
            licence=licence_snapshot(resolve_licence(settings)),
        )
    except (ComplianceError, LicenceError) as exc:
        # Reaching here means no report could be *built* at all — no
        # signing key, typically. That is a different thing from a report
        # that records bad news, which succeeds.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    document = generated.document
    chain = document.get("chain_verification", {})
    chain_ok = chain.get("ok") is True
    return ComplianceReportGeneratedResponse(
        counter=generated.counter,
        content_sha256=generated.content_sha256,
        chain_ok=chain_ok,
        generated_at=str(document.get("generated_at", "")),
        warning=(
            None
            if chain_ok
            else (
                "This install's own audit-record check FAILED, and the report says so "
                "— which is the point of generating it. Send it to your supplier and "
                "investigate; a hardware fault looks quite different from tampering."
            )
        ),
    )


@router.get("/admin/compliance/reports/{counter}/document")
async def download_compliance_report(
    counter: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    fmt: str = "json",
) -> Response:
    """Download an archived report as ``json`` or as its ``html`` twin.

    Served from the archive rather than regenerated, so what the customer
    reads is byte-for-byte what was signed. Regenerating would produce a
    different document under a different counter.
    """
    from matofx.compliance import render_report_html  # noqa: PLC0415
    from matofx.compliance.orm import ComplianceReport  # noqa: PLC0415

    await _require(db, current_user.id, Permission.AUDIT_READ)

    report = (
        await db.execute(sa.select(ComplianceReport).where(ComplianceReport.counter == counter))
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No compliance report numbered {counter}.",
        )

    document = dict(report.document or {})
    install = document.get("install", {})
    install_id = install.get("install_id", "install") if isinstance(install, dict) else "install"
    stem = f"compliance-{install_id}-{counter:04d}"

    if fmt == "html":
        return Response(
            content=render_report_html(document),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}.html"'},
        )
    return Response(
        content=json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{stem}.json"'},
    )
