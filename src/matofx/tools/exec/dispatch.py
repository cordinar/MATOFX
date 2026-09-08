"""Per-kind dispatch targets that route to a transport-specific backend.

The runner's :class:`~matofx.runner.registry.TargetRegistry` is keyed by
:data:`ExecutionTargetKind` (``local`` / ``aos`` / ``hpc``), but each of
``aos`` / ``hpc`` has **two** concrete transports — AOS over SSH or WinRM,
HPC via Slurm or MS-HPC — and every concrete target services exactly one
transport (it raises if handed a row for the other). These dispatch
targets occupy the single ``"aos"`` / ``"hpc"`` registry slot, read the
host / cluster row to learn its transport / scheduler, and forward every
call to the matching backend.

Both dispatchers implement the :class:`ExecutionTarget` protocol, so the
supervisor treats them like any other target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from matofx.db.session import session_scope
from matofx.tools.exec.aos_ssh import AosTargetError, SshAosTarget
from matofx.tools.exec.aos_winrm import WinRmAosTarget
from matofx.tools.exec.base import ExecutionTarget, ExecutionTargetKind
from matofx.tools.exec.hpc_mshpc import MsHpcHpcTarget
from matofx.tools.exec.hpc_slurm import HpcTargetError, SlurmHpcTarget
from matofx.tools.exec_orm import AosHost, HpcCluster

if TYPE_CHECKING:
    from matofx.db.session import SessionFactory
    from matofx.secrets import SecretsStore
    from matofx.tools.exec.base import (
        JobObservation,
        LogsRef,
        ResourceRequest,
        SubmitResult,
        TargetContext,
    )
    from matofx.tools.exec.staging import StagedWorkingDir


class HpcDispatchTarget:
    """Routes an HPC job to the Slurm or MS-HPC backend by ``cluster.scheduler``."""

    target_kind: ExecutionTargetKind = "hpc"

    def __init__(self, *, session_factory: SessionFactory, secrets_store: SecretsStore) -> None:
        """Construct both HPC backends over the shared factory + secrets store."""
        self._factory = session_factory
        self._by_scheduler: dict[str, ExecutionTarget] = {
            "slurm": SlurmHpcTarget(session_factory=session_factory, secrets_store=secrets_store),
            "mshpc": MsHpcHpcTarget(session_factory=session_factory, secrets_store=secrets_store),
        }

    async def _select(self, context: TargetContext) -> ExecutionTarget:
        cluster_id = context.hpc_cluster_id
        if cluster_id is None:
            raise HpcTargetError(
                "This job is on the HPC backend but has no cluster assigned. "
                "Re-check the tool's execution target."
            )
        async with session_scope(self._factory) as session:
            cluster = await session.get(HpcCluster, cluster_id)
            scheduler = cluster.scheduler if cluster is not None else None
        if scheduler is None:
            raise HpcTargetError(f"HPC cluster #{cluster_id} not found.")
        target = self._by_scheduler.get(scheduler)
        if target is None:
            raise HpcTargetError(
                f"HPC cluster #{cluster_id} uses unsupported scheduler {scheduler!r}."
            )
        return target

    async def submit(
        self,
        *,
        staged: StagedWorkingDir,
        resource_request: ResourceRequest,
        context: TargetContext,
    ) -> SubmitResult:
        """Delegate submit to the scheduler-specific backend."""
        target = await self._select(context)
        return await target.submit(
            staged=staged, resource_request=resource_request, context=context
        )

    async def poll(
        self, *, staged: StagedWorkingDir, context: TargetContext
    ) -> JobObservation:
        """Delegate poll to the scheduler-specific backend."""
        target = await self._select(context)
        return await target.poll(staged=staged, context=context)

    async def cancel(
        self,
        *,
        submit_result: SubmitResult,
        staged: StagedWorkingDir,
        grace_seconds: int,
        context: TargetContext,
    ) -> None:
        """Delegate cancel to the scheduler-specific backend."""
        target = await self._select(context)
        await target.cancel(
            submit_result=submit_result,
            staged=staged,
            grace_seconds=grace_seconds,
            context=context,
        )

    async def fetch_logs(
        self, *, staged: StagedWorkingDir, context: TargetContext
    ) -> LogsRef:
        """Delegate fetch_logs to the scheduler-specific backend."""
        target = await self._select(context)
        return await target.fetch_logs(staged=staged, context=context)


class AosDispatchTarget:
    """Routes an AOS job to the SSH or WinRM backend by ``host.transport``."""

    target_kind: ExecutionTargetKind = "aos"

    def __init__(self, *, session_factory: SessionFactory, secrets_store: SecretsStore) -> None:
        """Construct both AOS backends over the shared factory + secrets store."""
        self._factory = session_factory
        self._by_transport: dict[str, ExecutionTarget] = {
            "ssh": SshAosTarget(session_factory=session_factory, secrets_store=secrets_store),
            "winrm": WinRmAosTarget(session_factory=session_factory, secrets_store=secrets_store),
        }

    async def _select(self, context: TargetContext) -> ExecutionTarget:
        host_id = context.aos_host_id
        if host_id is None:
            raise AosTargetError(
                "This job is on the always-on-server backend but has no host "
                "assigned. Re-check the tool's execution target."
            )
        async with session_scope(self._factory) as session:
            host = await session.get(AosHost, host_id)
            transport = host.transport if host is not None else None
        if transport is None:
            raise AosTargetError(f"Always-on server #{host_id} not found.")
        target = self._by_transport.get(transport)
        if target is None:
            raise AosTargetError(
                f"Always-on server #{host_id} uses unsupported transport {transport!r}."
            )
        return target

    async def submit(
        self,
        *,
        staged: StagedWorkingDir,
        resource_request: ResourceRequest,
        context: TargetContext,
    ) -> SubmitResult:
        """Delegate submit to the transport-specific backend."""
        target = await self._select(context)
        return await target.submit(
            staged=staged, resource_request=resource_request, context=context
        )

    async def poll(
        self, *, staged: StagedWorkingDir, context: TargetContext
    ) -> JobObservation:
        """Delegate poll to the transport-specific backend."""
        target = await self._select(context)
        return await target.poll(staged=staged, context=context)

    async def cancel(
        self,
        *,
        submit_result: SubmitResult,
        staged: StagedWorkingDir,
        grace_seconds: int,
        context: TargetContext,
    ) -> None:
        """Delegate cancel to the transport-specific backend."""
        target = await self._select(context)
        await target.cancel(
            submit_result=submit_result,
            staged=staged,
            grace_seconds=grace_seconds,
            context=context,
        )

    async def fetch_logs(
        self, *, staged: StagedWorkingDir, context: TargetContext
    ) -> LogsRef:
        """Delegate fetch_logs to the transport-specific backend."""
        target = await self._select(context)
        return await target.fetch_logs(staged=staged, context=context)


__all__ = ["AosDispatchTarget", "HpcDispatchTarget"]
