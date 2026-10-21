"""ExportService + ImportService — the DB-aware migration surface.

Wraps the pure bundle / signing layer with reads from the Tool /
ToolVersion / Workflow / WorkflowMember / AosHost / HpcCluster /
PythonEnv / StorageLocation / User / Team tables, and on the import side
re-resolves every by-name reference against the destination install.

* :mod:`matofx.migrate.bundle` does pack / unpack and hash checks.
* :mod:`matofx.migrate.signing` does sign / verify.
* :mod:`matofx.migrate.trust` decides who is trusted.
* :mod:`matofx.migrate.errors` carries every plain-English string.

This module's job is to:

1. Walk the install's actual ORM rows and serialise them into the
   bundle's data shapes (export side).
2. Walk the unpacked-bundle's data shapes and reconstruct ORM rows
   on the destination (import side), per-item-atomically, applying the
   same reference checks a normal install applies (ADR-0023).
3. Reconcile workflow links (``downstream_tools`` ⇄ wired inputs) across
   the bundle and the tools already on the destination — never removing a
   link, adding the missing half on whichever side exists.
4. Emit :class:`ProgressEvent`s as it goes so the UI can render the
   live status log.
5. Audit ``migrate.bundle.exported`` / ``migrate.bundle.imported``
   once per run, carrying the bundle hash and per-item outcomes.

Per-item atomicity uses SQLAlchemy SAVEPOINTs (``session.begin_nested``).
A single item failing — for a planned reason *or an unexpected error* —
rolls back its savepoint; the run keeps going so later items still land.
**Preview (dry-run)** runs the real import inside one outer savepoint and
rolls that back at the end, so a workflow sees the tools imported earlier
in the same preview and link repairs are computed by the code that applies
them. Disk writes and the import audit row are skipped in preview.

Disk-side writes (tool bundles, workflow icons) happen *after* the item's
savepoint commits, so a rolled-back item never leaves orphan files.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from sqlalchemy import delete as sa_delete
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
    SurrogateToolNotExportableError,
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
from matofx.tools.errors import UnknownMaterialClassError, UnknownMaterialPropertyError
from matofx.tools.exec_orm import AosHost, HpcCluster, PythonEnv, StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.tools.service import WORLD_TEAM_NAME, make_tool_service
from matofx.tools.versioning import compare_versions, compute_bundle_hash_from_files
from matofx.users.orm import User
from matofx.users.team_orm import Team
from matofx.workflows.errors import UpstreamOrderingError
from matofx.workflows.orm import (
    Workflow,
    WorkflowKind,
    WorkflowMember,
    WorkflowVersion,
)
from matofx.workflows.service import check_upstream_ordering

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.migrate.trust import TrustedPeerService

_log = logging.getLogger(__name__)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# Descriptor filenames a tool bundle-on-disk may carry (mirrors
# matofx.tools.loader._DESCRIPTOR_FILENAMES). The export drops any of these
# and writes a fresh, authoritative descriptor.json from the DB — the on-disk
# copy is only the original *authoring* artefact and drifts from the DB after
# metadata / promotion version bumps (which update the DB, not the file).
_DESCRIPTOR_BUNDLE_FILENAMES: tuple[str, ...] = ("descriptor.toml", "descriptor.json")
_AUTHORITATIVE_DESCRIPTOR_FILENAME = "descriptor.json"

# Tool settings carried in the opaque ``settings`` dict beyond the descriptor
# (bundle v2, ADR-0023). Absent in a v1 bundle → the destination keeps its own
# value (an update) or the column default (a new tool).
_RUN_CAP_KEY = "run_cap"
_OPT_START_APPROVAL_KEY = "optimisation_start_approval_required"


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

    link_repairs: list[dict[str, str]] = field(default_factory=list)
    """Tools already on this install whose ``downstream_tools`` the import
    extended so a link into an imported tool works (ADR-0023 case 2)."""

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


@dataclass(frozen=True, slots=True)
class ExportSelection:
    """The tools a bundle will carry once required tools are added.

    ``added`` lists the ``(alias, version)`` pairs the *Include required
    tools* option pulled in beyond what was ticked — workflow members and
    the upstream tools selected tools are wired from — so the caller can say
    what else went into the bundle.
    """

    tool_specs: list[tuple[str, str]]
    added: list[tuple[str, str]]


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _wired_inputs(descriptor: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Inputs of ``descriptor`` that take another tool's output."""
    out: list[dict[str, Any]] = []
    for inp in (descriptor or {}).get("inputs") or []:
        if not isinstance(inp, dict) or not inp.get("downstream_input"):
            continue
        upstream = inp.get("upstream_tool")
        if isinstance(upstream, str) and upstream:
            out.append(inp)
    return out


def _output_names(descriptor: dict[str, Any] | None) -> set[str]:
    return {
        str(o.get("name"))
        for o in (descriptor or {}).get("outputs") or []
        if isinstance(o, dict) and o.get("name")
    }


def _downstream(descriptor: dict[str, Any] | None) -> list[str]:
    return [str(a) for a in (descriptor or {}).get("downstream_tools") or [] if a]


def _without_downstream(descriptor: dict[str, Any] | None) -> dict[str, Any]:
    """The descriptor minus ``downstream_tools``.

    ``downstream_tools`` is a permission reconciled *additively* on import, so
    two descriptors that differ only there are the same content: re-importing
    a bundle after its list was extended must stay a no-op, not a conflict.
    """
    out = dict(descriptor or {})
    out.pop("downstream_tools", None)
    return out


def _without_icon(descriptor: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(descriptor or {})
    out.pop("icon_filename", None)
    return out


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
    workflow_icons_dir: Path | None = None
    """Where workflow icons live (``workflows.icons_dir``). ``None`` → icons
    are left out of the bundle (the CLI / tests may not configure it)."""

    async def resolve_selection(
        self,
        session: AsyncSession,
        *,
        tool_specs: list[tuple[str, str]],
        workflow_aliases: list[str],
        include_required: bool,
    ) -> ExportSelection:
        """Add the tools the selection needs to work on another install.

        With ``include_required``: every member of a selected workflow and,
        transitively, every upstream tool a carried tool's inputs are wired
        from — at its current version. Downstream tools are not added; their
        declarations are kept and link up when they are imported later.
        """
        specs = list(dict.fromkeys(tool_specs))
        if not include_required:
            return ExportSelection(tool_specs=specs, added=[])
        carried = {alias for alias, _ in specs}
        queue: list[str] = []
        for alias in workflow_aliases:
            workflow = await self._get_admin_workflow(session, alias)
            queue.extend(m.tool_alias for m in await self._workflow_members(session, workflow.id))
        for alias, version in specs:
            tool = await self._get_tool(session, alias, version)
            queue.extend(str(i["upstream_tool"]) for i in _wired_inputs(tool.descriptor))
        added: list[tuple[str, str]] = []
        while queue:
            alias = queue.pop(0)
            if alias in carried:
                continue
            row = (
                await session.execute(select(Tool).where(Tool.alias == alias))
            ).scalar_one_or_none()
            if row is None or row.archived_at is not None:
                continue  # a dangling link stays a link; the import reports it
            carried.add(alias)
            added.append((row.alias, row.version))
            queue.extend(str(i["upstream_tool"]) for i in _wired_inputs(row.descriptor))
        return ExportSelection(tool_specs=specs + added, added=added)

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

        ``tool_specs`` are ``(alias, version)`` pairs; ``workflow_aliases``
        name admin workflows (custom workflows are not exportable). Run
        :meth:`resolve_selection` first to add required tools. Surrogate
        tools are refused (:class:`SurrogateToolNotExportableError`).
        Returns the signed gzipped-tarball bytes.
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
            if (tool.descriptor or {}).get("surrogate_model_version_id") is not None:
                raise SurrogateToolNotExportableError(tool.alias)
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
            versions = {
                t.alias: t.version
                for t in (
                    await session.execute(
                        select(Tool).where(Tool.alias.in_([m.tool_alias for m in members]))
                    )
                ).scalars()
            }
            entry = WorkflowManifestEntry(
                alias=workflow.alias,
                name=workflow.name,
                state_on_source=workflow.state,
                definition={
                    "version": workflow.version,
                    "descriptor": dict(workflow.descriptor or {}),
                    "members": [
                        {
                            "tool_alias": m.tool_alias,
                            "position": m.position,
                            # Informational (bundle v2): the destination notes
                            # a member whose version there differs.
                            "tool_version": versions.get(m.tool_alias),
                        }
                        for m in members
                    ],
                },
                # Workflow-level approver list is reserved in the bundle
                # schema for forward-compat; v2 workflows store
                # approvers on member tools, not on the workflow itself.
                approvers=[],
                icon_png_b64=self._read_workflow_icon(workflow),
            )
            out.append(WorkflowPayload(entry=entry))
            emit(
                on_event,
                info(f"Read workflow {workflow.alias} ({len(members)} member tool(s))."),
            )
        return out

    def _read_workflow_icon(self, workflow: Workflow) -> str | None:
        filename = (workflow.descriptor or {}).get("icon_filename")
        if not filename or self.workflow_icons_dir is None:
            return None
        path = self.workflow_icons_dir / Path(str(filename)).name
        if not path.is_file():
            return None
        return base64.b64encode(path.read_bytes()).decode("ascii")

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
            # Metadata-tab settings that live in columns, not the descriptor.
            _RUN_CAP_KEY: tool.run_cap,
            _OPT_START_APPROVAL_KEY: bool(tool.optimisation_start_approval_required),
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


def make_export_service(
    audit: AuditWriter, *, workflow_icons_dir: Path | None = None
) -> ExportService:
    """Build the :class:`ExportService` from an audit writer."""
    return ExportService(audit=audit, workflow_icons_dir=workflow_icons_dir)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class _RunState:
    """What the tool pass hands to the link and workflow passes."""

    present: dict[str, dict[str, Any]] = field(default_factory=dict[str, Any])
    """alias → descriptor of every bundle tool now on this install (imported,
    updated or already identical)."""

    fresh: set[str] = field(default_factory=set)
    """Aliases this run created or updated (their rows may be amended in
    place without another version bump)."""

    link_repairs: list[dict[str, str]] = field(default_factory=list)


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

    workflow_icons_dir: Path | None = None
    """``workflows.icons_dir`` — where an imported workflow's icon is written.
    ``None`` → the icon is reported as not stored."""

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

        ``dry_run=True`` runs the whole import inside one savepoint that is
        rolled back before returning, so the preview reports exactly what a
        real import would do. Disk writes are skipped in dry-run mode.
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
        state = _RunState()
        outer = await session.begin_nested() if dry_run else None
        try:
            # 3. Per-tool import. Each item gets its own savepoint so a
            # single failure doesn't take down the whole bundle.
            emit(on_event, info("Importing tools..."))
            for payload in unpacked.tools:
                outcomes.append(
                    await self._try_import_tool(
                        session,
                        payload=payload,
                        actor_user_id=actor_user_id,
                        dry_run=dry_run,
                        state=state,
                        on_event=on_event,
                    )
                )

            # 4. Links: complete the other half of every link that touches
            # an imported tool, and name the ones that can't be completed.
            if state.present:
                emit(on_event, info("Checking workflow links..."))
                await self._reconcile_links(
                    session,
                    state=state,
                    actor_user_id=actor_user_id,
                    dry_run=dry_run,
                    on_event=on_event,
                )

            # 5. Per-workflow import. Members must resolve here (already
            # present, or imported in this same run).
            emit(on_event, info("Importing workflows..."))
            for wf_payload in unpacked.workflows:
                outcomes.append(
                    await self._try_import_workflow(
                        session,
                        payload=wf_payload,
                        actor_user_id=actor_user_id,
                        dry_run=dry_run,
                        state=state,
                        on_event=on_event,
                    )
                )
        finally:
            if outer is not None and outer.is_active:
                await outer.rollback()

        # 6. Summary line + mark_used + bundle-import audit.
        def _count(kind: str, outcome: str) -> int:
            return sum(1 for o in outcomes if o.kind == kind and o.outcome == outcome)

        for kind, title in (("tool", "Tools"), ("workflow", "Workflows")):
            emit(
                on_event,
                summary(
                    f"{title}: {_count(kind, 'imported')} imported, "
                    f"{_count(kind, 'skipped')} skipped, "
                    f"{_count(kind, 'no-op')} already present (no-op).",
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
                        "link_repairs": list(state.link_repairs),
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
            link_repairs=list(state.link_repairs),
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
        state: _RunState,
        on_event: OnEvent,
    ) -> ImportItemOutcome:
        entry = payload.entry
        savepoint = await session.begin_nested()
        try:
            action, descriptor = await self._import_tool_inner(
                session,
                payload=payload,
                actor_user_id=actor_user_id,
                on_event=on_event,
            )
            await savepoint.commit()
        except ItemSkippedError as exc:
            await savepoint.rollback()
            return self._tool_skipped(entry, exc.detail, on_event)
        except Exception as exc:  # one bad item must not sink the bundle
            await savepoint.rollback()
            _log.exception("Unexpected error importing tool %s %s", entry.alias, entry.version)
            return self._tool_skipped(
                entry,
                f"Tool {entry.alias} {entry.version}: unexpected error while importing "
                f"({type(exc).__name__}: {exc}). Nothing was changed for this tool.",
                on_event,
            )

        state.present[entry.alias] = descriptor
        if action != "no-op":
            state.fresh.add(entry.alias)
        # Disk writes happen *outside* the savepoint so a rolled-back
        # DB transaction never leaves orphan files. Dry-run skips them.
        if action != "no-op" and not dry_run:
            _write_bundle_dir(self.bundle_root / entry.alias / entry.version, payload.files)

        if action == "no-op":
            emit(
                on_event,
                info(
                    f"Tool {entry.alias} {entry.version}: already present "
                    "with identical content — skipping (no-op)."
                ),
            )
            return ImportItemOutcome(
                kind="tool", alias=entry.alias, version=entry.version, outcome="no-op"
            )

        verb = "updated" if action == "updated" else "imported"
        emit(
            on_event,
            imported_tool(
                alias=entry.alias,
                version=entry.version,
                message=f"Tool {entry.alias} {entry.version} {verb} (state: new).",
            ),
        )
        return ImportItemOutcome(
            kind="tool", alias=entry.alias, version=entry.version, outcome="imported"
        )

    @staticmethod
    def _tool_skipped(
        entry: ToolManifestEntry, reason: str, on_event: OnEvent
    ) -> ImportItemOutcome:
        emit(on_event, skipped_tool(alias=entry.alias, version=entry.version, reason=reason))
        return ImportItemOutcome(
            kind="tool", alias=entry.alias, version=entry.version, outcome="skipped", reason=reason
        )

    async def _import_tool_inner(  # noqa: PLR0912, PLR0915 — one rule per step, in order
        self,
        session: AsyncSession,
        *,
        payload: ToolPayload,
        actor_user_id: int,
        on_event: OnEvent,
    ) -> tuple[Literal["imported", "updated", "no-op"], dict[str, Any]]:
        """The actual DB-writes path. Wrapped in a SAVEPOINT by the caller.

        Returns the action taken and the descriptor now on this install.
        Raises :class:`ItemSkippedError` subclasses when the item can't be
        imported (rules: ``designs/migrate-completeness.md``).
        """
        entry = payload.entry
        settings = dict(entry.settings or {})
        descriptor_dict: dict[str, Any] = dict(settings.get("descriptor") or {})
        label = f"Tool {entry.alias} {entry.version}"

        # 0. Surrogate tools carry a source-install model id (ADR-0023 §3);
        # current exports refuse them, but a v1 bundle may still hold one.
        if descriptor_dict.get("surrogate_model_version_id") is not None:
            raise ItemSkippedError(
                kind="tool",
                alias=entry.alias,
                version=entry.version,
                detail=(
                    f"{label}: a surrogate tool runs a fitted model held on the source "
                    "install, which can't be moved yet. Re-create the surrogate here."
                ),
            )

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

        # 2. Teams: unknown names dropped; none left → teams pending.
        teams_pending = await self._resolve_teams(
            session, descriptor_dict, label, item="tool", on_event=on_event
        )

        # 3. Approvers: must exist, be active, and be allowed to approve.
        dropped = await self._filter_approver_usernames(session, descriptor_dict)
        if dropped:
            emit(
                on_event,
                warning(
                    f"{label}: dropped approver(s) "
                    + ", ".join(f"{u!r} ({why})" for u, why in dropped)
                    + "."
                ),
            )

        # 4. Re-validate the (possibly filtered) descriptor. If the
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
                    dropped_usernames=[u for u, _ in dropped],
                    approvals_policy=descriptor_dict.get("approvals_policy", "none"),
                ) from exc
            raise ItemSkippedError(
                kind="tool",
                alias=entry.alias,
                version=entry.version,
                detail=(
                    f"{label}: descriptor "
                    f"validation failed on this install. Underlying "
                    f"error: {exc.errors()[0]['msg'] if exc.errors() else exc}"
                ),
            ) from exc
        descriptor_dict = validated.model_dump()

        # 5. Material inputs — same rule as a normal install.
        try:
            await make_tool_service(self.audit).validate_material_references(session, validated)
        except (UnknownMaterialClassError, UnknownMaterialPropertyError) as exc:
            raise ItemSkippedError(
                kind="tool",
                alias=entry.alias,
                version=entry.version,
                detail=f"{label}: {exc} Add it under Admin → Materials database, then re-import.",
            ) from exc

        # 6. Licences can't be checked (no third-party licence registry).
        if validated.licences_required:
            names = ", ".join(f"{lic.name} (x{lic.count})" for lic in validated.licences_required)
            emit(
                on_event,
                info(
                    f"{label}: needs licence(s) {names}. This install can't check licence "
                    "availability automatically — confirm the licence manager provides them."
                ),
            )

        # 7. Version rules against an existing tool with this alias.
        existing_tool = await self._get_tool_row(session, entry.alias)
        if existing_tool is not None:
            if existing_tool.archived_at is not None:
                raise ItemSkippedError(
                    kind="tool",
                    alias=entry.alias,
                    version=entry.version,
                    detail=(
                        f"{label}: this tool is archived on this install. Unarchive it "
                        "first if you want the import to update it, then re-import."
                    ),
                )
            order = compare_versions(entry.version, existing_tool.version)
            if order == 0:
                if _descriptor_hash(_without_downstream(existing_tool.descriptor)) == (
                    _descriptor_hash(_without_downstream(descriptor_dict))
                ):
                    return "no-op", dict(existing_tool.descriptor or {})
                raise VersionHashConflictError(entry.alias, entry.version)
            if order < 0:
                raise ItemSkippedError(
                    kind="tool",
                    alias=entry.alias,
                    version=entry.version,
                    detail=(
                        f"{label}: this install already has the newer version "
                        f"{existing_tool.version}; importing would downgrade it. Export the "
                        "current version from the source, or leave this tool out."
                    ),
                )
            if existing_tool.state != "new":
                emit(
                    on_event,
                    warning(
                        f"{label}: replaces version {existing_tool.version}, which is "
                        f"'{existing_tool.state}' here. The tool goes back to 'new' and is "
                        "unavailable to users until you promote it again."
                    ),
                )

        # 8. Links (case 4): tools already here that take this tool's
        # outputs must appear in its downstream list.
        wired_here = await self._aliases_wired_from(session, entry.alias)
        current = _downstream(descriptor_dict)
        extra = [a for a in wired_here if a not in current]
        if extra:
            descriptor_dict["downstream_tools"] = current + extra
            emit(
                on_event,
                info(
                    f"{label}: also lists {', '.join(extra)} as downstream — "
                    "already wired to take its outputs on this install."
                ),
            )

        bundle_path = self.bundle_root / entry.alias / entry.version
        now = _utcnow()
        resource_request = settings.get("resource_request") or None
        requires_gpu = bool(settings.get("requires_gpu", False))
        execution_target = settings.get("execution_target", "local")

        action: Literal["imported", "updated"]
        if existing_tool is None:
            action = "imported"
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
            action = "updated"
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
        tool_row.teams_pending = teams_pending
        if _RUN_CAP_KEY in settings:
            run_cap = settings[_RUN_CAP_KEY]
            tool_row.run_cap = int(run_cap) if run_cap is not None else None
        if _OPT_START_APPROVAL_KEY in settings:
            tool_row.optimisation_start_approval_required = bool(settings[_OPT_START_APPROVAL_KEY])

        # 9. Append the immutable ToolVersion snapshot so Job submissions
        # have a row to FK to. The V&V cohort key is computed from the
        # bundle's own files — they reach disk only after this savepoint.
        session.add(
            ToolVersion(
                alias=entry.alias,
                version=entry.version,
                descriptor_snapshot=descriptor_dict,
                bundle_path=str(bundle_path),
                created_by_user_id=actor_user_id,
                change_summary=(
                    f"{'Updated' if action == 'updated' else 'Imported'} from migration "
                    f"bundle (source state: {entry.state_on_source or 'unknown'})"
                ),
                changes={},
                created_at=now,
                bundle_hash=compute_bundle_hash_from_files(payload.files),
            )
        )
        await session.flush()
        return action, descriptor_dict

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    async def _reconcile_links(
        self,
        session: AsyncSession,
        *,
        state: _RunState,
        actor_user_id: int,
        dry_run: bool,
        on_event: OnEvent,
    ) -> None:
        """Complete or report every link touching a tool from this bundle.

        A link A → B lives on both tools: ``A.downstream_tools ∋ B`` and B's
        inputs naming A as ``upstream_tool``. Never remove one; add the
        missing half on whichever side exists; name the ones that can't be
        completed (``designs/migrate-completeness.md`` §links).
        """
        handled: set[tuple[str, str]] = set()
        for b_alias, b_desc in list(state.present.items()):
            for inp in _wired_inputs(b_desc):
                a_alias = str(inp["upstream_tool"])
                output = inp.get("upstream_output")
                field_label = str(inp.get("label") or inp.get("name") or "?")
                a_desc: dict[str, Any] | None = state.present.get(a_alias)
                a_row = None if a_desc is not None else await self._get_tool_row(session, a_alias)
                if a_desc is None and a_row is None:
                    emit(
                        on_event,
                        warning(
                            f"Tool {b_alias}: input {field_label!r} is wired to tool "
                            f"{a_alias!r}, which this install doesn't have. It can't take "
                            f"{a_alias}'s output until {a_alias} is imported (it can still "
                            "be entered by hand)."
                        ),
                    )
                    continue
                source_desc = a_desc if a_desc is not None else dict(a_row.descriptor or {})  # type: ignore[union-attr]
                if output and output not in _output_names(source_desc):
                    emit(
                        on_event,
                        warning(
                            f"Tool {b_alias}: input {field_label!r} takes output {output!r} "
                            f"from {a_alias}, but {a_alias} has no such output here. Re-pick "
                            f"the output on {b_alias}'s Edit Tool page."
                        ),
                    )
                if (a_alias, b_alias) in handled or b_alias in _downstream(source_desc):
                    continue
                handled.add((a_alias, b_alias))
                await self._add_downstream(
                    session,
                    a_alias=a_alias,
                    b_alias=b_alias,
                    state=state,
                    actor_user_id=actor_user_id,
                    dry_run=dry_run,
                    on_event=on_event,
                )
            for d_alias in _downstream(b_desc):
                if d_alias in state.present:
                    continue
                if await self._get_tool_row(session, d_alias) is None:
                    emit(
                        on_event,
                        info(
                            f"Tool {b_alias}: lists {d_alias!r} as a downstream tool, which "
                            "this install doesn't have yet — the link completes when it is "
                            "imported."
                        ),
                    )

    async def _add_downstream(
        self,
        session: AsyncSession,
        *,
        a_alias: str,
        b_alias: str,
        state: _RunState,
        actor_user_id: int,
        dry_run: bool,
        on_event: OnEvent,
    ) -> None:
        """Make tool A list B downstream (the half of a link that's missing)."""
        row = await self._get_tool_row(session, a_alias)
        if row is None:
            return
        if row.archived_at is not None:
            emit(
                on_event,
                warning(
                    f"Tool {b_alias} takes inputs from {a_alias}, which is archived here — "
                    f"{a_alias} was left unchanged."
                ),
            )
            return
        new_list = [*_downstream(row.descriptor), b_alias]
        savepoint = await session.begin_nested()
        try:
            if a_alias in state.fresh:
                # Created/updated by this run: complete its list in place —
                # no second version bump for a tool that just arrived.
                row.descriptor = {**dict(row.descriptor or {}), "downstream_tools": new_list}
                await session.flush()
                old_version = new_version = row.version
            else:
                old_version = row.version
                descriptor = ToolDescriptor.model_validate(
                    {**dict(row.descriptor or {}), "downstream_tools": new_list}
                )
                updated = await make_tool_service(self.audit).update_descriptor(
                    session, actor_user_id=actor_user_id, descriptor=descriptor
                )
                new_version = updated.version
            await savepoint.commit()
        except Exception as exc:  # a repair failure is a warning, not a skip
            await savepoint.rollback()
            emit(
                on_event,
                warning(
                    f"Couldn't add {b_alias} to {a_alias}'s downstream tools ({exc}). Add it "
                    f"on {a_alias}'s Edit Tool page so {b_alias} can take its outputs."
                ),
            )
            return
        if a_alias in state.present:
            state.present[a_alias] = dict(row.descriptor or {})
        if a_alias in state.fresh:
            emit(on_event, info(f"Tool {a_alias}: also lists {b_alias} as downstream."))
            return
        state.link_repairs.append(
            {
                "tool": a_alias,
                "added_downstream": b_alias,
                "from_version": old_version,
                "to_version": new_version,
            }
        )
        verb = "Would also update" if dry_run else "Also updated"
        emit(
            on_event,
            info(
                f"{verb} tool {a_alias} ({old_version} → {new_version}): it now lists "
                f"{b_alias} as a downstream tool, so {b_alias}'s inputs can take its outputs."
            ),
        )

    async def _aliases_wired_from(self, session: AsyncSession, alias: str) -> list[str]:
        """Non-archived tools here whose inputs take ``alias``'s outputs."""
        rows = (await session.execute(select(Tool).where(Tool.archived_at.is_(None)))).scalars()
        return sorted(
            t.alias
            for t in rows
            if t.alias != alias
            and any(i.get("upstream_tool") == alias for i in _wired_inputs(t.descriptor))
        )

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
        state: _RunState,
        on_event: OnEvent,
    ) -> ImportItemOutcome:
        entry = payload.entry
        savepoint = await session.begin_nested()
        try:
            action, icon_bytes = await self._import_workflow_inner(
                session,
                payload=payload,
                actor_user_id=actor_user_id,
                state=state,
                on_event=on_event,
            )
            await savepoint.commit()
        except ItemSkippedError as exc:
            await savepoint.rollback()
            return self._workflow_skipped(entry.alias, exc.detail, on_event)
        except Exception as exc:  # one bad item must not sink the bundle
            await savepoint.rollback()
            _log.exception("Unexpected error importing workflow %s", entry.alias)
            return self._workflow_skipped(
                entry.alias,
                f"Workflow {entry.alias!r}: unexpected error while importing "
                f"({type(exc).__name__}: {exc}). Nothing was changed for this workflow.",
                on_event,
            )

        if action == "no-op":
            emit(
                on_event,
                info(
                    f"Workflow {entry.alias!r}: already present with identical content — "
                    "skipping (no-op)."
                ),
            )
            return ImportItemOutcome(
                kind="workflow", alias=entry.alias, version=None, outcome="no-op"
            )

        if icon_bytes is not None and not dry_run and self.workflow_icons_dir is not None:
            try:
                self.workflow_icons_dir.mkdir(parents=True, exist_ok=True)
                (self.workflow_icons_dir / f"{entry.alias}.png").write_bytes(icon_bytes)
            except OSError as exc:
                emit(
                    on_event,
                    warning(f"Workflow {entry.alias!r}: its icon couldn't be saved ({exc})."),
                )

        emit(
            on_event,
            imported_workflow(
                alias=entry.alias,
                message=f"Workflow {entry.alias!r} {action} (state: new).",
            ),
        )
        return ImportItemOutcome(
            kind="workflow", alias=entry.alias, version=None, outcome="imported"
        )

    @staticmethod
    def _workflow_skipped(alias: str, reason: str, on_event: OnEvent) -> ImportItemOutcome:
        emit(on_event, skipped_workflow(alias=alias, reason=reason))
        return ImportItemOutcome(
            kind="workflow", alias=alias, version=None, outcome="skipped", reason=reason
        )

    async def _import_workflow_inner(  # noqa: PLR0912, PLR0915 — one rule per step, in order
        self,
        session: AsyncSession,
        *,
        payload: WorkflowPayload,
        actor_user_id: int,
        state: _RunState,
        on_event: OnEvent,
    ) -> tuple[Literal["imported", "updated", "no-op"], bytes | None]:
        entry = payload.entry
        definition = entry.definition or {}
        members = sorted(
            (m for m in definition.get("members") or [] if isinstance(m, dict)),
            key=lambda m: int(m.get("position") or 0),
        )
        version = str(definition.get("version") or "1.0.0")
        descriptor = dict(definition.get("descriptor") or {})
        label = f"Workflow {entry.alias!r}"

        # 1. Every member must resolve: imported in this run, or already here.
        tool_aliases: list[str] = []
        descriptors: dict[str, Any] = {}
        names: dict[str, str] = {}
        for member in members:
            tool_alias = member.get("tool_alias")
            if not tool_alias:
                raise ItemSkippedError(
                    kind="workflow",
                    alias=entry.alias,
                    version=None,
                    detail=f"{label} has a member with no tool_alias. The bundle is malformed.",
                )
            tool_aliases.append(str(tool_alias))
            if tool_alias in state.present:
                descriptors[tool_alias] = state.present[tool_alias]
            else:
                row = await self._get_tool_row(session, tool_alias)
                if row is None:
                    raise WorkflowDependencyMissingError(entry.alias, tool_alias)
                if row.archived_at is not None:
                    raise ItemSkippedError(
                        kind="workflow",
                        alias=entry.alias,
                        version=None,
                        detail=(
                            f"{label}: member tool {tool_alias!r} is archived on this "
                            "install. Unarchive it, then re-import."
                        ),
                    )
                descriptors[tool_alias] = dict(row.descriptor or {})
            names[tool_alias] = str(descriptors[tool_alias].get("name") or tool_alias)
            bundle_version = member.get("tool_version")
            here_version = descriptors[tool_alias].get("version")
            if bundle_version and here_version and bundle_version != here_version:
                emit(
                    on_event,
                    info(
                        f"{label}: member {names[tool_alias]} is version {here_version} "
                        f"here (the workflow was exported alongside {bundle_version})."
                    ),
                )

        # 2. Validated like a save in the workflow editor.
        try:
            check_upstream_ordering(tool_aliases, descriptors=descriptors, names=names)
        except UpstreamOrderingError as exc:
            raise ItemSkippedError(
                kind="workflow", alias=entry.alias, version=None, detail=f"{label}: {exc}"
            ) from exc

        # 3. Teams, as for tools.
        teams_pending = await self._resolve_teams(
            session, descriptor, label, item="workflow", on_event=on_event
        )

        # 4. Icon (bundle v2).
        icon_bytes: bytes | None = None
        if entry.icon_png_b64:
            try:
                icon_bytes = base64.b64decode(entry.icon_png_b64, validate=True)
            except (binascii.Error, ValueError):
                emit(on_event, warning(f"{label}: its icon in the bundle is unreadable — skipped."))
            if icon_bytes is not None and self.workflow_icons_dir is None:
                emit(
                    on_event,
                    warning(
                        f"{label}: no workflow icon folder is configured, so its icon wasn't kept."
                    ),
                )
                icon_bytes = None
        if icon_bytes is not None:
            descriptor["icon_filename"] = f"{entry.alias}.png"

        # 5. Existing alias: update / no-op / conflict / downgrade.
        existing = (
            await session.execute(select(Workflow).where(Workflow.alias == entry.alias))
        ).scalar_one_or_none()
        source_state = entry.state_on_source or "unknown"
        if existing is not None:
            if existing.kind != WorkflowKind.ADMIN.value:
                raise WorkflowAliasCollisionError(entry.alias)
            if existing.archived_at is not None:
                raise ItemSkippedError(
                    kind="workflow",
                    alias=entry.alias,
                    version=None,
                    detail=(
                        f"{label}: this workflow is archived on this install. Unarchive it "
                        "first if you want the import to update it, then re-import."
                    ),
                )
            existing_members = [
                m.tool_alias
                for m in (
                    await session.execute(
                        select(WorkflowMember)
                        .where(WorkflowMember.workflow_id == existing.id)
                        .order_by(WorkflowMember.position.asc())
                    )
                ).scalars()
            ]
            order = compare_versions(version, existing.version)
            if order == 0:
                if (
                    existing.name == entry.name
                    and existing_members == tool_aliases
                    and _without_icon(existing.descriptor) == _without_icon(descriptor)
                ):
                    return "no-op", None
                raise ItemSkippedError(
                    kind="workflow",
                    alias=entry.alias,
                    version=None,
                    detail=(
                        f"{label} {version} already exists here with different content. "
                        "Bump the workflow's version on the source (edit and save it), "
                        "export again, and re-import."
                    ),
                )
            if order < 0:
                raise ItemSkippedError(
                    kind="workflow",
                    alias=entry.alias,
                    version=None,
                    detail=(
                        f"{label}: this install already has the newer version "
                        f"{existing.version}; importing {version} would downgrade it."
                    ),
                )
            if existing.state != "new":
                emit(
                    on_event,
                    warning(
                        f"{label}: replaces version {existing.version}, which is "
                        f"'{existing.state}' here. The workflow goes back to 'new' and is "
                        "unavailable to users until you promote it again."
                    ),
                )
            if icon_bytes is None and (existing.descriptor or {}).get("icon_filename"):
                descriptor["icon_filename"] = existing.descriptor["icon_filename"]
            existing.name = entry.name
            existing.descriptor = descriptor
            existing.version = version
            existing.state = "new"
            existing.teams_pending = teams_pending
            existing.updated_at = _utcnow()
            await session.execute(
                sa_delete(WorkflowMember).where(WorkflowMember.workflow_id == existing.id)
            )
            await session.flush()
            workflow_row = existing
            action: Literal["imported", "updated"] = "updated"
        else:
            workflow_row = Workflow(
                alias=entry.alias,
                name=entry.name,
                version=version,
                kind=WorkflowKind.ADMIN.value,
                state="new",  # always 'new' on cross-install import
                descriptor=descriptor,
                installed_by_user_id=actor_user_id,
                teams_pending=teams_pending,
            )
            session.add(workflow_row)
            await session.flush()
            action = "imported"

        for position, tool_alias in enumerate(tool_aliases, start=1):
            session.add(
                WorkflowMember(
                    workflow_id=workflow_row.id, tool_alias=tool_alias, position=position
                )
            )
        session.add(
            WorkflowVersion(
                alias=workflow_row.alias,
                version=workflow_row.version,
                descriptor_snapshot=dict(descriptor),
                members_snapshot=[
                    {"tool_alias": a, "position": i} for i, a in enumerate(tool_aliases, start=1)
                ],
                created_by_user_id=actor_user_id,
                change_summary=(
                    f"{'Updated' if action == 'updated' else 'Imported'} from migration "
                    f"bundle (source state: {source_state})"
                ),
                changes={},
            )
        )
        await session.flush()
        return action, icon_bytes

    # ------------------------------------------------------------------
    # By-name reference resolution
    # ------------------------------------------------------------------

    async def _resolve_teams(
        self,
        session: AsyncSession,
        descriptor_dict: dict[str, Any],
        label: str,
        *,
        item: str,
        on_event: OnEvent,
    ) -> bool:
        """Keep the teams that exist here; return ``True`` when none did.

        Mutates ``descriptor_dict["teams"]``. An empty list on the source
        (= everyone) is left alone. When the source named teams and none
        exist here, the item lands with no teams and *teams pending*: it
        can't leave ``new`` until an admin chooses its teams (ADR-0023 §4).
        """
        raw = [str(t).strip() for t in descriptor_dict.get("teams") or [] if str(t).strip()]
        if not raw:
            return False
        real = [t for t in raw if t != WORLD_TEAM_NAME]
        found: set[str] = set()
        if real:
            found = set(
                (await session.execute(select(Team.name).where(Team.name.in_(real)))).scalars()
            )
        kept = [t for t in raw if t == WORLD_TEAM_NAME or t in found]
        dropped = [t for t in raw if t not in kept]
        descriptor_dict["teams"] = kept
        if dropped and kept:
            emit(
                on_event,
                warning(
                    f"{label}: team(s) {', '.join(repr(t) for t in dropped)} don't exist on "
                    "this install and were dropped."
                ),
            )
        if not kept:
            emit(
                on_event,
                warning(
                    f"{label}: none of its teams ({', '.join(repr(t) for t in dropped)}) exist "
                    f"on this install, so it was imported with no teams. The {item} can't be "
                    "promoted out of 'new' until you set its teams on its edit page."
                ),
            )
            return True
        return False

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
    ) -> list[tuple[str, str]]:
        """Keep approvers who exist, are active, and may approve runs.

        Mutates ``descriptor_dict`` in place — the same three conditions a
        normal install enforces. Returns ``(username, reason)`` for each one
        dropped so the caller can report them (and skip the tool if the list
        is now empty under a non-``none`` policy).
        """
        raw = descriptor_dict.get("approver_usernames") or []
        if not raw:
            return []
        cleaned = [u.strip().lower() for u in raw if u]
        if not cleaned:
            descriptor_dict["approver_usernames"] = []
            return [(str(u), "blank") for u in raw]
        rows = (await session.execute(select(User).where(User.username.in_(cleaned)))).scalars()
        by_username = {u.username: u for u in rows}
        kept: list[str] = []
        dropped: list[tuple[str, str]] = []
        for username in cleaned:
            user = by_username.get(username)
            if user is None:
                dropped.append((username, "doesn't exist here"))
            elif not user.is_active:
                dropped.append((username, "deactivated"))
            elif not user.is_approver:
                dropped.append((username, "'Can approve runs' is off"))
            else:
                kept.append(username)
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
    workflow_icons_dir: Path | None = None,
) -> ImportService:
    """Wire up the :class:`ImportService` from its dependencies."""
    return ImportService(
        audit=audit,
        trust_service=trust_service,
        bundle_root=bundle_root,
        workflow_icons_dir=workflow_icons_dir,
    )


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
    payload = descriptor or {}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
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
    "ExportSelection",
    "ExportService",
    "ImportItemOutcome",
    "ImportResult",
    "ImportService",
    "make_export_service",
    "make_import_service",
]
