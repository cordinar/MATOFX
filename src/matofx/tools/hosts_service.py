"""Admin services for the aos_host + hpc_cluster registries.

These are the execution-target host registries operators maintain to
point MATOFX at always-on servers (AOS) and HPC clusters. Like the
storage_location / python_env registries (:mod:`matofx.tools.paths_service`)
each row is row-versioned and never hard-deleted — deactivate instead so
historical jobs keep resolving.

The connection ``transport`` is **derived** from the host's OS / the
cluster's scheduler (linux→ssh / windows→winrm; slurm→ssh / mshpc→winrm),
matching the DB CHECK constraints, so callers never pass an inconsistent
pair. The actual credential lives in the secrets store under
``secret_ref`` (see :mod:`matofx.secrets` + ``matofx secrets``); these
services only manage the non-secret row.

Permissions: :attr:`Permission.AOS_HOSTS_MANAGE` /
:attr:`Permission.HPC_CLUSTERS_MANAGE`, seeded on the administrator role
by migration 0012.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.tools.exec_orm import AosHost, HpcCluster
from matofx.tools.paths_service import DuplicateNameError, NotFoundError
from matofx.users.errors import PermissionDeniedError
from matofx.users.permissions import Permission, user_has_permission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter

AosOs = Literal["linux", "windows"]
HpcScheduler = Literal["slurm", "mshpc"]

_AOS_TRANSPORT: dict[str, str] = {"linux": "ssh", "windows": "winrm"}
_HPC_TRANSPORT: dict[str, str] = {"slurm": "ssh", "mshpc": "winrm"}
_DEFAULT_PORT: dict[str, int] = {"ssh": 22, "winrm": 5986}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _clean(value: str | None) -> str | None:
    """Normalise an optional free-text field to ``None`` or a stripped value.

    A whitespace-only value from a cleared form field must land as
    ``NULL``, not as ``" "`` — these columns feed scheduler flags, and
    an empty-looking string would emit a flag with no argument.
    """
    if value is None:
        return None
    return value.strip() or None


# ---------- AosHost ------------------------------------------------------


@dataclass(frozen=True)
class AosHostsService:
    """CRUD for the ``aos_host`` registry."""

    audit: AuditWriter

    async def list_all(self, session: AsyncSession) -> list[AosHost]:
        """Return every host, oldest first."""
        stmt = select(AosHost).order_by(AosHost.id.asc())
        return list((await session.execute(stmt)).scalars())

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        hostname: str,
        os: AosOs,
        username: str,
        secret_ref: str,
        port: int | None = None,
        max_concurrency: int = 1,
        cpu_cores: int | None = None,
        memory_mb: int | None = None,
    ) -> AosHost:
        """Register a new always-on server. Requires ``aos_hosts.manage``."""
        await self._require(session, actor_user_id)
        transport = _AOS_TRANSPORT[os]
        row = AosHost(
            name=name.strip(),
            hostname=hostname.strip(),
            os=os,
            transport=transport,
            port=port if port is not None else _DEFAULT_PORT[transport],
            username=username.strip(),
            secret_ref=secret_ref.strip(),
            max_concurrency=max_concurrency,
            cpu_cores=cpu_cores,
            memory_mb=memory_mb,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("always-on server", name) from exc
        await self._record(session, actor_user_id, "create", row)
        return row

    async def update(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        row_id: int,
        hostname: str,
        os: AosOs,
        username: str,
        secret_ref: str,
        port: int,
        max_concurrency: int,
        cpu_cores: int | None,
        memory_mb: int | None,
    ) -> AosHost:
        """Overwrite the editable fields of a host (name is immutable)."""
        await self._require(session, actor_user_id)
        row = await session.get(AosHost, row_id)
        if row is None:
            raise NotFoundError("always-on server", row_id)
        row.hostname = hostname.strip()
        row.os = os
        row.transport = _AOS_TRANSPORT[os]
        row.username = username.strip()
        row.secret_ref = secret_ref.strip()
        row.port = port
        row.max_concurrency = max_concurrency
        row.cpu_cores = cpu_cores
        row.memory_mb = memory_mb
        await session.flush()
        await self._record(session, actor_user_id, "update", row)
        return row

    async def set_active(
        self, session: AsyncSession, *, actor_user_id: int, row_id: int, is_active: bool
    ) -> AosHost:
        """Activate / deactivate a host. Deactivated hosts accept no new jobs."""
        await self._require(session, actor_user_id)
        row = await session.get(AosHost, row_id)
        if row is None:
            raise NotFoundError("always-on server", row_id)
        if row.is_active != is_active:
            row.is_active = is_active
            await session.flush()
            action = "activate" if is_active else "deactivate"
            await self._record(session, actor_user_id, action, row)
        return row

    async def _require(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.AOS_HOSTS_MANAGE):
            raise PermissionDeniedError(Permission.AOS_HOSTS_MANAGE.value)

    async def _record(
        self, session: AsyncSession, actor_user_id: int, verb: str, row: AosHost
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=f"admin.aos_host.{verb}",
                resource_type="aos_host",
                resource_id=str(row.id),
                outcome="success",
                payload={"name": row.name, "hostname": row.hostname, "transport": row.transport},
            ),
        )


def make_aos_hosts_service(audit: AuditWriter) -> AosHostsService:
    """Build the :class:`AosHostsService`."""
    return AosHostsService(audit=audit)


# ---------- HpcCluster ---------------------------------------------------


@dataclass(frozen=True)
class HpcClustersService:
    """CRUD for the ``hpc_cluster`` registry."""

    audit: AuditWriter

    async def list_all(self, session: AsyncSession) -> list[HpcCluster]:
        """Return every cluster, oldest first."""
        stmt = select(HpcCluster).order_by(HpcCluster.id.asc())
        return list((await session.execute(stmt)).scalars())

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        head_node: str,
        scheduler: HpcScheduler,
        username: str,
        secret_ref: str,
        port: int | None = None,
        default_queue: str | None = None,
        account: str | None = None,
        wckey: str | None = None,
    ) -> HpcCluster:
        """Register a new HPC cluster. Requires ``hpc_clusters.manage``."""
        await self._require(session, actor_user_id)
        transport = _HPC_TRANSPORT[scheduler]
        row = HpcCluster(
            name=name.strip(),
            scheduler=scheduler,
            head_node=head_node.strip(),
            transport=transport,
            port=port if port is not None else _DEFAULT_PORT[transport],
            username=username.strip(),
            secret_ref=secret_ref.strip(),
            default_queue=_clean(default_queue),
            account=_clean(account),
            wckey=_clean(wckey),
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("HPC cluster", name) from exc
        await self._record(session, actor_user_id, "create", row)
        return row

    async def update(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        row_id: int,
        head_node: str,
        scheduler: HpcScheduler,
        username: str,
        secret_ref: str,
        port: int,
        default_queue: str | None,
        account: str | None = None,
        wckey: str | None = None,
    ) -> HpcCluster:
        """Overwrite the editable fields of a cluster (name is immutable)."""
        await self._require(session, actor_user_id)
        row = await session.get(HpcCluster, row_id)
        if row is None:
            raise NotFoundError("HPC cluster", row_id)
        row.scheduler = scheduler
        row.head_node = head_node.strip()
        row.transport = _HPC_TRANSPORT[scheduler]
        row.username = username.strip()
        row.secret_ref = secret_ref.strip()
        row.port = port
        row.default_queue = _clean(default_queue)
        row.account = _clean(account)
        row.wckey = _clean(wckey)
        await session.flush()
        await self._record(session, actor_user_id, "update", row)
        return row

    async def set_active(
        self, session: AsyncSession, *, actor_user_id: int, row_id: int, is_active: bool
    ) -> HpcCluster:
        """Activate / deactivate a cluster."""
        await self._require(session, actor_user_id)
        row = await session.get(HpcCluster, row_id)
        if row is None:
            raise NotFoundError("HPC cluster", row_id)
        if row.is_active != is_active:
            row.is_active = is_active
            await session.flush()
            action = "activate" if is_active else "deactivate"
            await self._record(session, actor_user_id, action, row)
        return row

    async def _require(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.HPC_CLUSTERS_MANAGE):
            raise PermissionDeniedError(Permission.HPC_CLUSTERS_MANAGE.value)

    async def _record(
        self, session: AsyncSession, actor_user_id: int, verb: str, row: HpcCluster
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=f"admin.hpc_cluster.{verb}",
                resource_type="hpc_cluster",
                resource_id=str(row.id),
                outcome="success",
                payload={"name": row.name, "head_node": row.head_node, "scheduler": row.scheduler},
            ),
        )


def make_hpc_clusters_service(audit: AuditWriter) -> HpcClustersService:
    """Build the :class:`HpcClustersService`."""
    return HpcClustersService(audit=audit)
