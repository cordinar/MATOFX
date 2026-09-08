"""Admin services for the storage_location + python_env registries.

Both are small lookup tables admins maintain through the "Execution
environments" page. Each row is row-versioned for optimistic locking
and never hard-deleted (set ``is_active=False`` and, for storage
locations, ``retired_at``-stamped) — old jobs continue to resolve
the historical references.

Permissions:

- :attr:`Permission.STORAGE_LOCATIONS_MANAGE` for the storage_location
  surface;
- :attr:`Permission.PYTHON_ENVS_MANAGE` for the python_env surface.

Both are seeded on the administrator role by migration 0012.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.tools.exec_orm import PythonEnv, StorageLocation
from matofx.users.errors import PermissionDeniedError
from matofx.users.permissions import Permission, user_has_permission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class PathsAdminError(RuntimeError):
    """Surface error from the paths-admin service.

    Plain English; safe to render directly to admins via the UI.
    """


class DuplicateNameError(PathsAdminError):
    """A row with the supplied name already exists in this registry."""

    def __init__(self, registry: str, name: str) -> None:
        """Construct with the registry kind and the offending name."""
        super().__init__(
            f"A {registry} entry called {name!r} already exists. Please choose a different name."
        )
        self.registry = registry
        self.name = name


class NotFoundError(PathsAdminError):
    """The requested row id doesn't exist in this registry."""

    def __init__(self, registry: str, row_id: int) -> None:
        """Construct with the registry kind and the missing id."""
        super().__init__(f"We couldn't find that {registry}.")
        self.registry = registry
        self.row_id = row_id


# ---------- StorageLocation ----------------------------------------------


@dataclass(frozen=True)
class StorageLocationsService:
    """CRUD for the ``storage_location`` registry."""

    audit: AuditWriter

    async def list_all(self, session: AsyncSession) -> list[StorageLocation]:
        """Return every row, oldest first."""
        stmt = select(StorageLocation).order_by(StorageLocation.id.asc())
        return list((await session.execute(stmt)).scalars())

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        mount_path: str,
    ) -> StorageLocation:
        """Register a new shared mount. Requires ``storage_locations.manage``."""
        await self._require(session, actor_user_id)
        row = StorageLocation(name=name.strip(), mount_path=mount_path.strip())
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("storage location", name) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.storage_location.create",
                resource_type="storage_location",
                resource_id=str(row.id),
                outcome="success",
                payload={"name": row.name, "mount_path": row.mount_path},
            ),
        )
        return row

    async def retire(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        row_id: int,
    ) -> StorageLocation:
        """Mark a row inactive + stamp ``retired_at``. Audit-only; no FK breaks."""
        await self._require(session, actor_user_id)
        row = await session.get(StorageLocation, row_id)
        if row is None:
            raise NotFoundError("storage location", row_id)
        if row.retired_at is not None:
            return row
        row.is_active = False
        row.retired_at = _utcnow()
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.storage_location.retire",
                resource_type="storage_location",
                resource_id=str(row.id),
                outcome="success",
                payload={"name": row.name},
            ),
        )
        return row

    async def _require(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(
            session, actor_user_id, Permission.STORAGE_LOCATIONS_MANAGE
        ):
            raise PermissionDeniedError(Permission.STORAGE_LOCATIONS_MANAGE.value)


def make_storage_locations_service(audit: AuditWriter) -> StorageLocationsService:
    """Build the :class:`StorageLocationsService`."""
    return StorageLocationsService(audit=audit)


# ---------- PythonEnv ----------------------------------------------------


@dataclass(frozen=True)
class PythonEnvsService:
    """CRUD for the ``python_env`` registry."""

    audit: AuditWriter

    async def list_all(self, session: AsyncSession) -> list[PythonEnv]:
        """Return every row, oldest first."""
        stmt = select(PythonEnv).order_by(PythonEnv.id.asc())
        return list((await session.execute(stmt)).scalars())

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        interpreter_path: str,
        storage_location_id: int | None = None,
    ) -> PythonEnv:
        """Register a new Python interpreter. Requires ``python_envs.manage``."""
        await self._require(session, actor_user_id)
        row = PythonEnv(
            name=name.strip(),
            interpreter_path=interpreter_path.strip(),
            storage_location_id=storage_location_id,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("python env", name) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.python_env.create",
                resource_type="python_env",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "name": row.name,
                    "interpreter_path": row.interpreter_path,
                    "storage_location_id": row.storage_location_id,
                },
            ),
        )
        return row

    async def deactivate(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        row_id: int,
    ) -> PythonEnv:
        """Flip ``is_active=False`` so new tools can't pick this env. Audited."""
        await self._require(session, actor_user_id)
        row = await session.get(PythonEnv, row_id)
        if row is None:
            raise NotFoundError("python env", row_id)
        if not row.is_active:
            return row
        row.is_active = False
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.python_env.deactivate",
                resource_type="python_env",
                resource_id=str(row.id),
                outcome="success",
                payload={"name": row.name},
            ),
        )
        return row

    async def _require(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.PYTHON_ENVS_MANAGE):
            raise PermissionDeniedError(Permission.PYTHON_ENVS_MANAGE.value)


def make_python_envs_service(audit: AuditWriter) -> PythonEnvsService:
    """Build the :class:`PythonEnvsService`."""
    return PythonEnvsService(audit=audit)
