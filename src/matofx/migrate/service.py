"""ExportService + ImportService — the DB-aware migration surface.

Wraps the pure bundle / signing layer with reads from the Tool /
ToolVersion / Workflow / WorkflowMember / AosHost / HpcCluster /
PythonEnv / StorageLocation / User tables, and on the import side
re-resolves every by-name reference against the destination install.

Designed to be thin: most of the work lives elsewhere.

* :mod:`matofx.migrate.bundle` does pack / unpack and hash checks.
* :mod:`matofx.migrate.signing` does sign / verify.
* :mod:`matofx.migrate.trust` decides who is trusted.
* :mod:`matofx.migrate.errors` carries every plain-English string.

This module's job is to:

1. Walk the install's actual ORM rows and serialise them into the
   bundle's data shapes (export side).
2. Walk the unpacked-bundle's data shapes and reconstruct ORM rows
   on the destination (import side), per-item-atomically.
3. Emit :class:`ProgressEvent`s as it goes so the UI can render the
   live status log.
4. Audit ``migrate.bundle.exported`` / ``migrate.bundle.imported``
   once per run, carrying the bundle hash and per-item outcomes.

Per-item atomicity uses SQLAlchemy SAVEPOINTs (``session.begin_nested``).
A single item failing rolls back its savepoint; the outer transaction
keeps going so subsequent items still have a chance to land. Dry-run
mode runs the same code path but rolls back the outer transaction at
the end — operator gets the full preview without writing anything.

Disk-side writes for tool bundles happen *after* the per-item
savepoint commits. The order is: (1) validate, (2) insert ORM rows
inside savepoint, (3) commit savepoint, (4) write bundle files to
disk. A power failure between (3) and (4) leaves a DB row pointing
at a partial bundle dir, which the audit log captures and the
operator can resolve with a re-import. Real two-phase commit is a
v1.x ask, not a v1 ask.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.migrate.bundle import (
    ToolManifestEntry,
    ToolPayload,
    WorkflowManifestEntry,
    WorkflowPayload,
    pack_bundle,
    unpack_bundle,
)
from matofx.migrate.errors import (
    ApproverListEmptyError,
    BundleUntrustedError,
    ItemSkippedError,
    MissingReferenceError,
    ToolNotFoundForExportError,
    VersionHashConflictError,
    WorkflowAliasCollisionError,
    WorkflowDependencyMissingError,
    WorkflowNotFoundForExportError,
)
from matofx.migrate.progress import (
    OnEvent,
    emit,
    imported_tool,
    imported_workflow,
    info,
    skipped_tool,
    skipped_workflow,
    summary,
    warning,
)
from matofx.migrate.signing import (
    KeyPair,
    compute_fingerprint,
    display_fingerprint,
    public_key_from_hex,
    public_key_to_hex,
    sign_manifest,
    verify_manifest,
)
from matofx.tools.descriptor import ToolDescriptor
from matofx.tools.exec_orm import AosHost, HpcCluster, PythonEnv, StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.users.orm import User
from matofx.workflows.orm import (
    Workflow,
    WorkflowKind,
    WorkflowMember,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.migrate.trust import TrustedPeerService


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# Descriptor filenames a tool bundle-on-disk may carry (mirrors
# matofx.tools.loader._DESCRIPTOR_FILENAMES). The export drops any of these
# and writes a fresh, authoritative descriptor.json from the DB — the on-disk
# copy is only the original *authoring* artefact and drifts from the DB after
# metadata / promotion version bumps (which update the DB, not the file).
_DESCRIPTOR_BUNDLE_FILENAMES: tuple[str, ...] = ("descriptor.toml", "descriptor.json")
_AUTHORITATIVE_DESCRIPTOR_FILENAME = "descriptor.json"


def _authoritative_descriptor_bytes(descriptor: dict[str, Any] | None) -> bytes:
    """Serialise the DB descriptor blob to deterministic, readable JSON bytes."""
    return (
        json.dumps(dict(descriptor or {}), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


ItemOutcome = Literal["imported", "skipped", "no-op"]


@dataclass(frozen=True, slots=True)
class ImportItemOutcome:
    """One row in :attr:`ImportResult.items` — what happened to one item."""

    kind: Literal["tool", "workflow"]
    alias: str
    version: str | None
    outcome: ItemOutcome
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    """The outcome of one bundle import.

    Returned by :meth:`ImportService.import_bundle`, and mirrors what the UI
    summarises and what ``migrate.bundle.imported`` audits.
    """

    source_fingerprint: str
    """Canonical (lowercase, no separators) hex."""

    source_label: str
    """Human label from the destination's trust list."""

    bundle_hash: str
    """SHA-256 of the bundle bytes (hex)."""

    notes: str
    """Free-text notes the exporter typed on the source."""

    created_at_utc: _dt.datetime
    """When the bundle was exported on the source."""

    created_by: str
    """Identity (typically email) of the source-side exporter."""

    items: list[ImportItemOutcome] = field(default_factory=list)
    dry_run: bool = False

    @property
    def imported_count(self) -> int:
        """How many items were imported."""
        return sum(1 for i in self.items if i.outcome == "imported")

    @property
    def skipped_count(self) -> int:
        """How many items were skipped, with a reason recorded against each."""
        return sum(1 for i in self.items if i.outcome == "skipped")

    @property
    def noop_count(self) -> int:
        """How many items were already present and needed no change."""
        return sum(1 for i in self.items if i.outcome == "no-op")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportService:
    """Read installed tools + workflows and produce a signed bundle.

    Construct one per process; cheap and stateless. Audit writer
    injected at construction so every export run lands one
    ``migrate.bundle.exported`` row in the chain.
    """

    audit: AuditWriter

    async def build_bundle(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        key_pair: KeyPair,
        source_install_id: str,
        tool_specs: list[tuple[str, str]],
        workflow_aliases: list[str],
        notes: str = "",
        actor_label: str | None = None,
        on_event: OnEvent = None,
    ) -> bytes:
        """Build a signed migration bundle from the listed items.

        Parameters
        ----------
        session
            DB session for reads + the export audit row.
        actor_user_id
            User triggering the export. Recorded in the audit payload
            and embedded in the manifest's ``created_by`` field when
            ``actor_label`` is omitted (falls back to ``"user:<id>"``).
        key_pair
            The install's Ed25519 key pair. The public half is embedded
            in the manifest; the private half signs it.
        source_install_id
            Free-form install identifier (typically the install's UUID).
            Informational — verification rides on the fingerprint, not
            the install id.
        tool_specs
            ``(alias, version)`` pairs to include.
        workflow_aliases
            Aliases of admin-released workflows to include. Custom
            workflows are not exportable in v1 — the service refuses
            silently if the alias resolves to a ``kind='custom'`` row.

        Notes:
            Optional free-text exporter note. Shown in the destination's
            import preview.
        actor_label
            Override for the manifest's ``created_by`` field. Usually
            the exporting user's email or username. Defaults to
            ``"user:<id>"``.
        on_event
            Optional :class:`ProgressEvent` sink.

        Returns:
        -------
        bytes
            The signed gzipped-tarball bytes. Caller (router / CLI)
            decides where to write them.
        """
        emit(on_event, info("Reading tools and workflows from this install..."))

        tools = await self._collect_tools(session, tool_specs, on_event=on_event)
        workflows = await self._collect_workflows(session, workflow_aliases, on_event=on_event)

        fingerprint = compute_fingerprint(key_pair.public_key)
        public_key_hex = public_key_to_hex(key_pair.public_key)

        def signer(manifest_bytes: bytes) -> bytes:
            return sign_manifest(manifest_bytes, key_pair.private_key)

        emit(on_event, info("Packing and signing bundle..."))
        bundle_bytes = pack_bundle(
            source_install_id=source_install_id,
            source_fingerprint=fingerprint,
            source_public_key_hex=public_key_hex,
            created_by=actor_label or f"user:{actor_user_id}",
            notes=notes,
            tools=tools,
            workflows=workflows,
            signer=signer,
        )

        bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
        tool_summary = [f"{t.entry.alias}:{t.entry.version}" for t in tools]
        workflow_summary = [w.entry.alias for w in workflows]
        emit(
            on_event,
            summary(
                f"Bundle ready: {len(tools)} tool(s), "
                f"{len(workflows)} workflow(s). Bundle hash "
                f"{bundle_hash[:16]}...",
                bundle_hash=bundle_hash,
            ),
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="migrate.bundle.exported",
                resource_type="migrate_bundle",
                resource_id=bundle_hash,
                outcome="success",
                payload={
                    "bundle_hash": bundle_hash,
                    "fingerprint": fingerprint,
                    "tools": tool_summary,
                    "workflows": workflow_summary,
                    "notes": notes,
                },
            ),
        )
        return bundle_bytes

    # ------------------------------------------------------------------
    # Per-kind collection helpers
    # ------------------------------------------------------------------

    async def _collect_tools(
        self,
        session: AsyncSession,
        tool_specs: list[tuple[str, str]],
        *,
        on_event: OnEvent,  # noqa: ARG002 - part of the resolver signature the caller dispatches on
    ) -> list[ToolPayload]:
        out: list[ToolPayload] = []
        for alias, version in tool_specs:
            tool = await self._get_tool(session, alias, version)
            settings = await self._serialise_tool_settings(session, tool)
            files = _read_bundle_dir(Path(tool.bundle_path))
            # The on-disk descriptor file is only the original authoring
            # artefact and drifts from the DB after version bumps. Drop it and
            # write the authoritative descriptor from the DB so the bundle is
            # self-consistent (the import reads settings.descriptor, not this
            # file, but a stale file is misleading to anyone inspecting it).
            for name in _DESCRIPTOR_BUNDLE_FILENAMES:
                files.pop(name, None)
            files[_AUTHORITATIVE_DESCRIPTOR_FILENAME] = _authoritative_descriptor_bytes(
                tool.descriptor
            )
            entry = ToolManifestEntry(
                alias=tool.alias,
                version=tool.version,
                settings=settings,
                state_on_source=tool.state,
                files={},  # filled by pack_bundle
            )
            out.append(ToolPayload(entry=entry, files=files))
            emit(
                on_event,
                info(f"Read tool {tool.alias} {tool.version} ({len(files)} file(s) in bundle)."),
            )
        return out

    async def _collect_workflows(
        self,
        session: AsyncSession,
        workflow_aliases: list[str],
        *,
        on_event: OnEvent,
    ) -> list[WorkflowPayload]:
        out: list[WorkflowPayload] = []
        for alias in workflow_aliases:
            workflow = await self._get_admin_workflow(session, alias)
            members = await self._workflow_members(session, workflow.id)
            entry = WorkflowManifestEntry(
                alias=workflow.alias,
                name=workflow.name,
                state_on_source=workflow.state,
                definition={
                    "version": workflow.version,
                    "descriptor": dict(workflow.descriptor or {}),
                    "members": [
                        {"tool_alias": m.tool_alias, "position": m.position} for m in members
                    ],
                },
                # Workflow-level approver list is reserved in the bundle
                # schema for forward-compat; v2 workflows store
                # approvers on member tools, not on the workflow itself.
                # See feedback_migrate_references_by_name.md.
                approvers=[],
            )
            out.append(WorkflowPayload(entry=entry))
            emit(
                on_event,
                info(f"Read workflow {workflow.alias} ({len(members)} member tool(s))."),
            )
        return out

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    async def _get_tool(self, session: AsyncSession, alias: str, version: str) -> Tool:
        stmt = select(Tool).where(Tool.alias == alias, Tool.version == version)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ToolNotFoundForExportError(alias, version)
        return row

    async def _get_admin_workflow(self, session: AsyncSession, alias: str) -> Workflow:
        stmt = select(Workflow).where(
            Workflow.alias == alias, Workflow.kind == WorkflowKind.ADMIN.value
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WorkflowNotFoundForExportError(alias)
        return row

    async def _workflow_members(
        self, session: AsyncSession, workflow_id: int
    ) -> list[WorkflowMember]:
        stmt = (
            select(WorkflowMember)
            .where(WorkflowMember.workflow_id == workflow_id)
            .order_by(WorkflowMember.position.asc())
        )
        return list((await session.execute(stmt)).scalars())

    async def _serialise_tool_settings(self, session: AsyncSession, tool: Tool) -> dict[str, Any]:
        """Translate a Tool row's ID-keyed FKs into the manifest's by-name shape.

        The caller can pass the result straight through to
        :class:`ToolManifestEntry.settings`.
        """
        settings: dict[str, Any] = {
            "execution_target": tool.execution_target,
            "requires_gpu": tool.requires_gpu,
            "resource_request": dict(tool.resource_request or {}),
            "descriptor": dict(tool.descriptor or {}),
        }
        if tool.aos_host_id is not None:
            host = await session.get(AosHost, tool.aos_host_id)
            settings["aos_host_name"] = host.name if host else None
        if tool.hpc_cluster_id is not None:
            cluster = await session.get(HpcCluster, tool.hpc_cluster_id)
            settings["hpc_cluster_name"] = cluster.name if cluster else None
        if tool.python_env_id is not None:
            env = await session.get(PythonEnv, tool.python_env_id)
            if env is not None:
                settings["python_env_name"] = env.name
                if env.storage_location_id is not None:
                    storage = await session.get(StorageLocation, env.storage_location_id)
                    settings["python_env_storage_location_name"] = storage.name if storage else None
        return settings


def make_export_service(audit: AuditWriter) -> ExportService:
    """Build the :class:`ExportService` from an audit writer."""
    return ExportService(audit=audit)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportService:
    """Verify a bundle and reconstruct each item on this install.

    Looks up the trust list and resolves references by name. Construct one per
    process. Audit + trust + bundle-root config
    injected at construction.
    """

    audit: AuditWriter
    trust_service: TrustedPeerService
    bundle_root: Path
    """Destination directory under which per-tool bundles are staged.
    Each imported tool ends up at ``<bundle_root>/<alias>/<version>/``."""

    async def import_bundle(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        bundle_bytes: bytes,
        on_event: OnEvent = None,
        dry_run: bool = False,
    ) -> ImportResult:
        """Verify, trust-check, then per-item-import the bundle.

        ``dry_run=True`` walks the entire flow, reports outcomes,
        and rolls back every DB write before returning. Disk writes
        are skipped entirely in dry-run mode.
        """
        # 1. Verify signature + parse manifest. Raises BundleError /
        # BundleSigningError on failure (these are *bundle-level*
        # errors; the caller renders them outside the per-item log).
        emit(on_event, info("Verifying signature..."))
        unpacked = unpack_bundle(bundle_bytes, verifier=_make_default_verifier())

        manifest = unpacked.manifest
        fingerprint = manifest.source_fingerprint
        bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()

        # 2. Trust-list check. Authentic but untrusted bundles are refused.
        peer = await self.trust_service.get_by_fingerprint(session, fingerprint)
        if peer is None:
            raise BundleUntrustedError(display_fingerprint(fingerprint))

        emit(
            on_event,
            info(
                f"Signed by {peer.label!r} "
                f"(fingerprint {display_fingerprint(fingerprint)}). Trusted."
            ),
        )
        emit(
            on_event,
            info(
                f"Bundle contents: {len(unpacked.tools)} tool(s), "
                f"{len(unpacked.workflows)} workflow(s). "
                f"Exported {manifest.created_at_utc.isoformat()} by "
                f"{manifest.created_by!r}."
            ),
        )

        outcomes: list[ImportItemOutcome] = []

        # 3. Per-tool import. Each item gets its own savepoint so a
        # single failure doesn't take down the whole bundle.
        emit(on_event, info("Importing tools..."))
        imported_tool_aliases: set[str] = set()
        for payload in unpacked.tools:
            outcome = await self._try_import_tool(
                session,
                payload=payload,
                actor_user_id=actor_user_id,
                dry_run=dry_run,
                on_event=on_event,
            )
            outcomes.append(outcome)
            if outcome.outcome == "imported":
                imported_tool_aliases.add(payload.entry.alias)

        # 4. Per-workflow import. Workflow imports depend on member
        # tools being resolvable on the destination (either already
        # present, or imported in this same run). Cascading skips are
        # the most common failure mode here.
        emit(on_event, info("Importing workflows..."))
        for wf_payload in unpacked.workflows:
            outcome = await self._try_import_workflow(
                session,
                payload=wf_payload,
                actor_user_id=actor_user_id,
                dry_run=dry_run,
                imported_tool_aliases=imported_tool_aliases,
                on_event=on_event,
            )
            outcomes.append(outcome)

        # 5. Summary line + mark_used + bundle-import audit.
        tool_imported = sum(1 for o in outcomes if o.kind == "tool" and o.outcome == "imported")
        tool_skipped = sum(1 for o in outcomes if o.kind == "tool" and o.outcome == "skipped")
        tool_noop = sum(1 for o in outcomes if o.kind == "tool" and o.outcome == "no-op")
        wf_imported = sum(1 for o in outcomes if o.kind == "workflow" and o.outcome == "imported")
        wf_skipped = sum(1 for o in outcomes if o.kind == "workflow" and o.outcome == "skipped")
        wf_noop = sum(1 for o in outcomes if o.kind == "workflow" and o.outcome == "no-op")

        emit(
            on_event,
            summary(
                f"Tools: {tool_imported} imported, {tool_skipped} skipped, "
                f"{tool_noop} already present (no-op).",
            ),
        )
        emit(
            on_event,
            summary(
                f"Workflows: {wf_imported} imported, {wf_skipped} skipped, "
                f"{wf_noop} already present (no-op).",
            ),
        )

        if not dry_run:
            await self.trust_service.mark_used(session, fingerprint=fingerprint)
            await self.audit.record(
                session,
                RecordInput(
                    actor=f"user:{actor_user_id}",
                    actor_type="user",
                    action="migrate.bundle.imported",
                    resource_type="migrate_bundle",
                    resource_id=bundle_hash,
                    outcome="success",
                    payload={
                        "bundle_hash": bundle_hash,
                        "source_fingerprint": fingerprint,
                        "source_label": peer.label,
                        "outcomes": [
                            {
                                "kind": o.kind,
                                "alias": o.alias,
                                "version": o.version,
                                "outcome": o.outcome,
                                "reason": o.reason,
                            }
                            for o in outcomes
                        ],
                    },
                ),
            )

        return ImportResult(
            source_fingerprint=fingerprint,
            source_label=peer.label,
            bundle_hash=bundle_hash,
            notes=manifest.notes,
            created_at_utc=manifest.created_at_utc,
            created_by=manifest.created_by,
            items=outcomes,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # Per-tool import
    # ------------------------------------------------------------------

    async def _try_import_tool(
        self,
        session: AsyncSession,
        *,
        payload: ToolPayload,
        actor_user_id: int,
        dry_run: bool,
        on_event: OnEvent,
    ) -> ImportItemOutcome:
        entry = payload.entry
        action: Literal["imported", "no-op"] | None = None
        savepoint = await session.begin_nested()
        try:
            action = await self._import_tool_inner(
                session,
                payload=payload,
                actor_user_id=actor_user_id,
                on_event=on_event,
            )
            if dry_run:
                # Roll back the SAVEPOINT (not the outer transaction)
                # so dry-run is read-only without losing the trust-list
                # lookup or the eventual migrate.bundle.imported audit row.
                await savepoint.rollback()
            else:
                await savepoint.commit()
        except ItemSkippedError as exc:
            await savepoint.rollback()
            emit(
                on_event,
                skipped_tool(alias=entry.alias, version=entry.version, reason=exc.detail),
            )
            return ImportItemOutcome(
                kind="tool",
                alias=entry.alias,
                version=entry.version,
                outcome="skipped",
                reason=exc.detail,
            )

        # Disk writes happen *outside* the savepoint so a rolled-back
        # DB transaction never leaves orphan files. Dry-run skips them.
        if action == "imported" and not dry_run:
            _write_bundle_dir(self.bundle_root / entry.alias / entry.version, payload.files)

        if action == "no-op":
            emit(
                on_event,
                info(
                    f"Tool {entry.alias} {entry.version}: already present "
                    "with identical descriptor — skipping (no-op)."
                ),
            )
            return ImportItemOutcome(
                kind="tool",
                alias=entry.alias,
                version=entry.version,
                outcome="no-op",
            )

        emit(
            on_event,
            imported_tool(
                alias=entry.alias,
                version=entry.version,
                message=(f"Tool {entry.alias} {entry.version} imported (state: new)."),
            ),
        )
        return ImportItemOutcome(
            kind="tool",
            alias=entry.alias,
            version=entry.version,
            outcome="imported",
        )

    async def _import_tool_inner(
        self,
        session: AsyncSession,
        *,
        payload: ToolPayload,
        actor_user_id: int,
        on_event: OnEvent,
    ) -> Literal["imported", "no-op"]:
        """The actual DB-writes path. Wrapped in a SAVEPOINT by the caller.

        Returns ``"no-op"`` when the (alias, version) already exists with
        an identical descriptor hash. Raises :class:`ItemSkippedError`
        subclasses when the item can't be imported.
        """
        entry = payload.entry
        settings = dict(entry.settings or {})
        descriptor_dict: dict[str, Any] = dict(settings.get("descriptor") or {})

        # 1. Resolve every by-name FK reference. A missing one is a
        # skip for this item — the operator must create the missing
        # registry row first.
        aos_host_id = await self._resolve_aos_host_id(session, settings.get("aos_host_name"), entry)
        hpc_cluster_id = await self._resolve_hpc_cluster_id(
            session, settings.get("hpc_cluster_name"), entry
        )
        python_env_id = await self._resolve_python_env_id(
            session,
            settings.get("python_env_name"),
            settings.get("python_env_storage_location_name"),
            entry,
        )

        # 2. Filter approver_usernames against destination users.
        dropped = await self._filter_approver_usernames(
            session, descriptor_dict, entry, on_event=on_event
        )
        if dropped:
            emit(
                on_event,
                warning(
                    f"Tool {entry.alias} {entry.version}: dropped "
                    f"{len(dropped)} approver username(s) that don't "
                    f"exist on this install ({', '.join(repr(u) for u in dropped)})."
                ),
            )

        # 3. Re-validate the (possibly filtered) descriptor. If the
        # validator now refuses (empty approver list with non-none
        # policy is the most likely reason), translate to the
        # plain-English skip reason.
        try:
            validated = ToolDescriptor.model_validate(descriptor_dict)
        except ValidationError as exc:
            if descriptor_dict.get(
                "approvals_policy", "none"
            ) != "none" and not descriptor_dict.get("approver_usernames"):
                raise ApproverListEmptyError(
                    alias=entry.alias,
                    version=entry.version,
                    dropped_usernames=dropped,
                    approvals_policy=descriptor_dict.get("approvals_policy", "none"),
                ) from exc
            raise ItemSkippedError(
                kind="tool",
                alias=entry.alias,
                version=entry.version,
                detail=(
                    f"Tool {entry.alias} {entry.version}: descriptor "
                    f"validation failed on this install. Underlying "
                    f"error: {exc.errors()[0]['msg'] if exc.errors() else exc}"
                ),
            ) from exc
        descriptor_dict = validated.model_dump()

        # 4. Idempotency / conflict check on (alias, version).
        existing_tool = await self._get_tool_row(session, entry.alias)
        if existing_tool is not None and existing_tool.version == entry.version:
            if _descriptor_hash(existing_tool.descriptor) == _descriptor_hash(descriptor_dict):
                return "no-op"
            raise VersionHashConflictError(entry.alias, entry.version)

        # 5. Compute the bundle path the row will point at.
        bundle_path = self.bundle_root / entry.alias / entry.version

        now = _utcnow()
        resource_request = settings.get("resource_request") or None
        requires_gpu = bool(settings.get("requires_gpu", False))
        execution_target = settings.get("execution_target", "local")

        if existing_tool is None:
            tool_row = Tool(
                alias=entry.alias,
                name=descriptor_dict.get("name") or entry.alias,
                version=entry.version,
                state="new",  # reset on every cross-install import
                descriptor=descriptor_dict,
                bundle_path=str(bundle_path),
                installed_by_user_id=actor_user_id,
                execution_target=execution_target,
                aos_host_id=aos_host_id,
                hpc_cluster_id=hpc_cluster_id,
                python_env_id=python_env_id,
                resource_request=resource_request,
                requires_gpu=requires_gpu,
            )
            session.add(tool_row)
        else:
            tool_row = existing_tool
            tool_row.name = descriptor_dict.get("name") or entry.alias
            tool_row.version = entry.version
            tool_row.state = "new"
            tool_row.descriptor = descriptor_dict
            tool_row.bundle_path = str(bundle_path)
            tool_row.execution_target = execution_target
            tool_row.aos_host_id = aos_host_id
            tool_row.hpc_cluster_id = hpc_cluster_id
            tool_row.python_env_id = python_env_id
            tool_row.resource_request = resource_request
            tool_row.requires_gpu = requires_gpu

        # 6. Append the immutable ToolVersion snapshot so Job submissions
        # have a row to FK to.
        version_row = ToolVersion(
            alias=entry.alias,
            version=entry.version,
            descriptor_snapshot=descriptor_dict,
            bundle_path=str(bundle_path),
            created_by_user_id=actor_user_id,
            change_summary=(
                f"Imported from migration bundle "
                f"(source state: {entry.state_on_source or 'unknown'})"
            ),
            changes={},
            created_at=now,
        )
        session.add(version_row)
        await session.flush()
        return "imported"

    # ------------------------------------------------------------------
    # Per-workflow import
    # ------------------------------------------------------------------

    async def _try_import_workflow(
        self,
        session: AsyncSession,
        *,
        payload: WorkflowPayload,
        actor_user_id: int,
        dry_run: bool,
        imported_tool_aliases: set[str],
        on_event: OnEvent,
    ) -> ImportItemOutcome:
        entry = payload.entry
        savepoint = await session.begin_nested()
        try:
            await self._import_workflow_inner(
                session,
                payload=payload,
                actor_user_id=actor_user_id,
                imported_tool_aliases=imported_tool_aliases,
            )
            if dry_run:
                await savepoint.rollback()
            else:
                await savepoint.commit()
        except ItemSkippedError as exc:
            await savepoint.rollback()
            emit(
                on_event,
                skipped_workflow(alias=entry.alias, reason=exc.detail),
            )
            return ImportItemOutcome(
                kind="workflow",
                alias=entry.alias,
                version=None,
                outcome="skipped",
                reason=exc.detail,
            )

        emit(
            on_event,
            imported_workflow(
                alias=entry.alias,
                message=(f"Workflow {entry.alias!r} imported (state: new)."),
            ),
        )
        return ImportItemOutcome(
            kind="workflow",
            alias=entry.alias,
            version=None,
            outcome="imported",
        )

    async def _import_workflow_inner(
        self,
        session: AsyncSession,
        *,
        payload: WorkflowPayload,
        actor_user_id: int,
        imported_tool_aliases: set[str],
    ) -> None:
        entry = payload.entry
        definition = entry.definition or {}
        members = definition.get("members") or []

        # 1. Alias collision check.
        existing = (
            await session.execute(select(Workflow).where(Workflow.alias == entry.alias))
        ).scalar_one_or_none()
        if existing is not None:
            raise WorkflowAliasCollisionError(entry.alias)

        # 2. Resolve every member tool. Each must either already exist on
        # the destination or have been imported in this same bundle.
        for member in members:
            tool_alias = member.get("tool_alias")
            if not tool_alias:
                raise ItemSkippedError(
                    kind="workflow",
                    alias=entry.alias,
                    version=None,
                    detail=(
                        f"Workflow {entry.alias!r} has a member with no "
                        "tool_alias. The bundle is malformed."
                    ),
                )
            if tool_alias in imported_tool_aliases:
                continue
            tool_row = await self._get_tool_row(session, tool_alias)
            if tool_row is None:
                raise WorkflowDependencyMissingError(entry.alias, tool_alias)

        # 3. Insert workflow + member rows. State is always 'new' on
        # cross-install import per feedback_migrate_reset_to_new.md.
        workflow_row = Workflow(
            alias=entry.alias,
            name=entry.name,
            version=definition.get("version") or "1.0.0",
            kind=WorkflowKind.ADMIN.value,
            state="new",
            descriptor=dict(definition.get("descriptor") or {}),
            installed_by_user_id=actor_user_id,
        )
        session.add(workflow_row)
        await session.flush()

        for member in members:
            session.add(
                WorkflowMember(
                    workflow_id=workflow_row.id,
                    tool_alias=member["tool_alias"],
                    position=int(member["position"]),
                )
            )
        await session.flush()

    # ------------------------------------------------------------------
    # By-name reference resolution
    # ------------------------------------------------------------------

    async def _resolve_aos_host_id(
        self,
        session: AsyncSession,
        name: str | None,
        entry: ToolManifestEntry,  # noqa: ARG002 - part of the resolver signature the caller dispatches on
    ) -> int | None:
        if not name:
            return None
        row = (
            await session.execute(select(AosHost).where(AosHost.name == name))
        ).scalar_one_or_none()
        if row is None:
            raise MissingReferenceError(
                alias=entry.alias,
                version=entry.version,
                ref_kind="AOS host",
                ref_name=name,
                where_to_create="Admin -> Execution Backends -> AOS Hosts",
            )
        return row.id

    async def _resolve_hpc_cluster_id(
        self,
        session: AsyncSession,
        name: str | None,
        entry: ToolManifestEntry,
    ) -> int | None:
        if not name:
            return None
        row = (
            await session.execute(select(HpcCluster).where(HpcCluster.name == name))
        ).scalar_one_or_none()
        if row is None:
            raise MissingReferenceError(
                alias=entry.alias,
                version=entry.version,
                ref_kind="HPC cluster",
                ref_name=name,
                where_to_create="Admin -> Execution Backends -> HPC Clusters",
            )
        return row.id

    async def _resolve_python_env_id(
        self,
        session: AsyncSession,
        env_name: str | None,
        storage_name: str | None,
        entry: ToolManifestEntry,
    ) -> int | None:
        if not env_name:
            return None
        env = (
            await session.execute(select(PythonEnv).where(PythonEnv.name == env_name))
        ).scalar_one_or_none()
        if env is None:
            raise MissingReferenceError(
                alias=entry.alias,
                version=entry.version,
                ref_kind="Python env",
                ref_name=env_name,
                where_to_create="Admin -> Execution environments -> Python envs",
            )
        # Source's PythonEnv had a chained reference to StorageLocation. We
        # don't override the destination's binding (the env-to-storage
        # link is a property of the destination's environment registry,
        # not the imported tool) but we do verify the storage location
        # is at least present so the operator notices a mismatch early.
        if storage_name:
            storage_row = (
                await session.execute(
                    select(StorageLocation).where(StorageLocation.name == storage_name)
                )
            ).scalar_one_or_none()
            if storage_row is None:
                raise MissingReferenceError(
                    alias=entry.alias,
                    version=entry.version,
                    ref_kind="storage location",
                    ref_name=storage_name,
                    where_to_create="Admin -> Execution environments -> Storage locations",
                )
        return env.id

    async def _filter_approver_usernames(
        self,
        session: AsyncSession,
        descriptor_dict: dict[str, Any],
        entry: ToolManifestEntry,  # noqa: ARG002 - part of the resolver signature the caller dispatches on
        *,
        on_event: OnEvent,  # noqa: ARG002 - part of the resolver signature the caller dispatches on
    ) -> list[str]:
        """Drop approver usernames that don't exist on the destination.

        Mutates ``descriptor_dict`` in place. Returns the list of dropped
        usernames so the caller can decide whether to skip the tool (if
        the resulting list is empty with a non-none policy).
        """
        raw = descriptor_dict.get("approver_usernames") or []
        if not raw:
            return []
        cleaned = [u.strip().lower() for u in raw if u]
        if not cleaned:
            descriptor_dict["approver_usernames"] = []
            return raw
        stmt = select(User.username).where(User.username.in_(cleaned))
        present = {u for (u,) in (await session.execute(stmt)).all()}
        kept = [u for u in cleaned if u in present]
        dropped = [u for u in cleaned if u not in present]
        descriptor_dict["approver_usernames"] = kept
        return dropped

    async def _get_tool_row(self, session: AsyncSession, alias: str) -> Tool | None:
        stmt = select(Tool).where(Tool.alias == alias)
        return (await session.execute(stmt)).scalar_one_or_none()


def make_import_service(
    audit: AuditWriter,
    *,
    trust_service: TrustedPeerService,
    bundle_root: Path,
) -> ImportService:
    """Wire up the :class:`ImportService` from its three deps."""
    return ImportService(audit=audit, trust_service=trust_service, bundle_root=bundle_root)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_bundle_dir(path: Path) -> dict[str, bytes]:
    """Walk ``path`` recursively and return ``{relative_path: bytes}``.

    Only regular files are picked up; symlinks and special files are
    skipped (they have no place in a tool bundle and the export-side
    refusal mirrors the import-side tarball validation).
    """
    if not path.exists():
        # An empty / missing bundle dir means the tool was installed
        # with no static files (rare but legitimate — see tutorials-only
        # tools). Return empty rather than failing.
        return {}
    out: dict[str, bytes] = {}
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        if child.is_symlink():
            continue
        rel = child.relative_to(path).as_posix()
        out[rel] = child.read_bytes()
    return out


def _write_bundle_dir(target: Path, files: dict[str, bytes]) -> None:
    """Write ``files`` into ``target``, creating directories as needed.

    Caller has already validated the relative paths via the bundle
    layer's path-traversal guard. No-op when ``files`` is empty.
    """
    if not files:
        return
    target.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _descriptor_hash(descriptor: dict[str, Any] | None) -> str:
    """SHA-256 of the canonical-JSON form of a descriptor.

    Used to decide whether a re-imported (alias, version) is byte-for-byte
    identical (no-op) or different (refuse with conflict).
    """
    import json  # noqa: PLC0415 - imported lazily to keep module import cheap

    payload = descriptor or {}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _make_default_verifier() -> Callable[[bytes, bytes, str], None]:
    """Build the default verifier callback for :func:`unpack_bundle`.

    The signing module is intentionally crypto-agnostic; this thin
    closure pulls in :class:`Ed25519PublicKey` and wires up the
    standard verification path. The import service uses this; tests
    can pass their own verifier for fault injection.
    """

    def verifier(manifest_bytes: bytes, signature: bytes, pub_hex: str) -> None:
        verify_manifest(manifest_bytes, signature, public_key_from_hex(pub_hex))

    return verifier


__all__ = [
    "ExportService",
    "ImportItemOutcome",
    "ImportResult",
    "ImportService",
    "make_export_service",
    "make_import_service",
]
