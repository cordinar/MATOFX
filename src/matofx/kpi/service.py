"""KPI extraction service (ADR-0012, slices 3 + 4).

Reads scalar KPI values out of a completed job's
:class:`OutputsDocument` and persists them to ``job_kpi_value`` for the
warehouse query path (slice 5) and the per-job HTML report headline
block (slice 8). Slice 4 adds :func:`reextract_tool_version` — the
admin / promote-triggered back-fill that re-reads ``outputs.json``
from durable storage for every completed job of a given tool version
and refreshes the ``job_kpi_value`` rows. The per-version lock lives
in :mod:`matofx.kpi._reextract_state`.

The single entry point :func:`extract_kpis_for_job` is called by the
runner's :meth:`Supervisor._post_complete` — see ``designs/kpi-
extraction.md`` §6. It runs **after** ``record_study_result`` (so a
parametric-study child's per-row results land first, in case anything
about KPI write fails) and **before** ``_apply_cleanup_for_tool`` (so
``outputs.json`` is still on disk when we read it).

**Never-raise contract** (design §5.1). The hook MUST NOT roll back
the job-completion transaction. Two guards land it:

1. Per-value classification (:func:`_classify_value`) never raises —
   missing / non-numeric / NaN / ±Inf each map to ``Decimal | None``
   without propagating the runtime quirk back up.
2. The whole extraction body runs inside ``session.begin_nested()`` —
   a SAVEPOINT — so an unexpected DB-level error (FK violation, etc.)
   rolls back just the KPI work without poisoning the outer
   transaction the runner uses to mark the job complete.

The :class:`KpiExtractResult` return value carries the per-KPI tally
the audit event records. Callers may inspect it, but the runner today
fires the function and continues without reading the return — the
audit chain is the durable record.

**Why a free function, not a method on a class.** Mirrors
``record_study_result``: the runner consumes a single async free
function; the API / UI consumes a service class with multiple
query methods. Slice 5's :class:`KpiService` will compose this
function for its re-extraction path.

**Why per-row UPSERT not INSERT-or-skip.** Re-extraction (slice 4)
overwrites the row when a descriptor edit changes the KPI set. The
UPSERT pattern matches that semantic and also makes the first-write
path idempotent under runner retries (the supervisor's heartbeat loop
can re-fire ``_post_complete`` on a recovered-from-stalled job). The
captured value + unit are replaced; ``extracted_at`` advances to
the latest write.

**Descriptor source.** The frozen :attr:`ToolVersion.descriptor_snapshot`
is the authority — the runner pinned ``job.tool_version_id`` at
submit, so a descriptor edit mid-flight cannot retroactively change
which outputs are KPIs. The live :attr:`Tool.descriptor` is *never*
read here.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.kpi import _reextract_state
from matofx.kpi.orm import JobKpiValue
from matofx.tools.exec.contracts import OutputsDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.hierarchy.orm import Job

_log = logging.getLogger("matofx.kpi")


# ---- Result shape ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class KpiExtractResult:
    """Per-job KPI extraction tally.

    - :attr:`extracted` — KPIs that produced a real ``Decimal`` value.
    - :attr:`missing` — KPIs that landed as ``value_numeric = NULL``
      (output key absent, non-numeric, NaN, ±Inf).
    - :attr:`total` — KPIs declared by the tool version's descriptor;
      always equals ``extracted + missing`` on a successful run.

    A failed-job code path (``outputs_doc is None``) returns
    ``KpiExtractResult(0, 0, 0)`` — no rows written; the audit event
    records that nothing was extracted.
    """

    extracted: int
    missing: int
    total: int


# ---- Value classification --------------------------------------------


def _classify_value(raw: Any) -> tuple[Decimal | None, str]:  # noqa: PLR0911 - a flat dispatch; splitting it would hide the mapping
    """Classify a runtime output value for KPI storage.

    Returns ``(value, reason)``. ``value`` is the captured
    :class:`Decimal` when classification succeeds, else ``None`` (the
    sentinel for warehouse "—" display). ``reason`` is the human-
    readable tag the WARNING log + audit event record on a sentinel.

    The conversion is **deliberately string-mediated** —
    ``Decimal(str(float_value))`` preserves the digits the tool
    actually wrote (with the float-format rounding the OS chose),
    rather than the binary-float drift ``Decimal(float_value)`` would
    introduce. See design §15.
    """
    if raw is None:
        return None, "missing"
    # ``bool`` is a subclass of ``int``; reject it explicitly so
    # ``kpi=true`` on a numeric output isn't silently satisfied by a
    # truthy flag value (which would round-trip as ``1`` / ``0`` and
    # nobody would notice the type confusion).
    if isinstance(raw, bool):
        return None, "non-numeric (bool)"
    if isinstance(raw, Decimal):
        if raw.is_nan():
            return None, "NaN"
        if raw.is_infinite():
            return None, "+Inf" if raw > 0 else "-Inf"
        return raw, "ok"
    if isinstance(raw, (int, float)):
        if isinstance(raw, float):
            if math.isnan(raw):
                return None, "NaN"
            if math.isinf(raw):
                return None, "+Inf" if raw > 0 else "-Inf"
        return Decimal(str(raw)), "ok"
    return None, f"non-numeric ({type(raw).__name__})"


# ---- Hook ------------------------------------------------------------


async def extract_kpis_for_job(
    audit: AuditWriter,
    session: AsyncSession,  # noqa: ARG001 - signature matches its sibling builders
    *,
    job: Job,
    outputs_doc: OutputsDocument | None,
) -> KpiExtractResult:
    """Persist one ``JobKpiValue`` row per declared KPI for this job.

    No-op when ``outputs_doc is None`` (the failure-path case — a job
    that didn't write valid outputs has no KPIs to surface). No-op
    when the tool version declares no KPIs (the common case for tools
    that haven't opted into the warehouse).

    Audited via ``kpi.extracted`` with the per-KPI tally. The hook is
    **best-effort** — see the module docstring's never-raise contract.

    Returns the :class:`KpiExtractResult` for callers that want to
    log / surface the tally; the runner discards the return value but
    the audit chain carries the durable record.
    """
    try:
        async with session.begin_nested():
            return await _extract_kpis_for_job_inner(
                audit, session, job=job, outputs_doc=outputs_doc
            )
    except Exception:
        _log.exception(
            "KPI extraction raised for job %s; swallowed (never-raise contract)",
            job.id,
        )
        return KpiExtractResult(extracted=0, missing=0, total=0)


async def _extract_kpis_for_job_inner(
    audit: AuditWriter,
    session: AsyncSession,
    *,
    job: Job,
    outputs_doc: OutputsDocument | None,
) -> KpiExtractResult:
    """Inner body — the SAVEPOINT-protected extraction logic.

    Split out so the never-raise wrapper above can keep its try/except
    minimal and so unit tests can target the happy-path logic without
    going through the SAVEPOINT machinery.
    """
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415 — break import cycle

    if outputs_doc is None:
        return KpiExtractResult(extracted=0, missing=0, total=0)

    tool_version = await session.get(ToolVersion, job.tool_version_id)
    if tool_version is None:
        # The FK from ``job.tool_version_id`` is RESTRICT, so a job's
        # tool_version cannot be deleted while the job exists — this
        # branch is unreachable in practice. The guard exists so a
        # tortured test fixture (or a future schema change) can't
        # raise inside the runner's never-raise path.
        return KpiExtractResult(extracted=0, missing=0, total=0)

    descriptor = tool_version.descriptor_snapshot
    if not isinstance(descriptor, dict):
        return KpiExtractResult(extracted=0, missing=0, total=0)
    output_fields = descriptor.get("outputs", [])
    if not isinstance(output_fields, list):
        return KpiExtractResult(extracted=0, missing=0, total=0)

    # Filter to declared KPIs. The OutputField validator
    # ``_kpi_consistency`` (slice 1) ensures ``kpi == True`` only
    # appears on ``type == "number"`` outputs — the type guard here is
    # a defence-in-depth check, not a real second classifier.
    kpi_fields = [
        f
        for f in output_fields
        if isinstance(f, dict) and f.get("kpi") is True and f.get("type") == "number"
    ]
    if not kpi_fields:
        return KpiExtractResult(extracted=0, missing=0, total=0)

    # Index OutputValues by name for O(1) lookup; outputs_doc.outputs
    # is a list in declaration order, but the descriptor's KPI
    # iteration also walks in declaration order, so this is O(N) total.
    values_by_name = {o.name: o for o in outputs_doc.outputs}

    extracted_now = _dt.datetime.now(tz=_dt.UTC)
    extracted_count = 0
    missing_count = 0

    for field in kpi_fields:
        name = str(field["name"])
        unit_raw = field.get("kpi_unit") if field.get("kpi_unit") is not None else field.get("unit")
        unit = str(unit_raw) if isinstance(unit_raw, str) else None

        runtime_value = values_by_name.get(name)
        raw = runtime_value.value if runtime_value is not None else None
        decimal_value, reason = _classify_value(raw)
        if decimal_value is None:
            missing_count += 1
            _log.warning(
                "KPI extract: job=%s kpi=%s value=NULL reason=%s",
                job.id,
                name,
                reason,
            )
        else:
            extracted_count += 1

        existing = (
            await session.execute(
                select(JobKpiValue).where(
                    JobKpiValue.job_id == job.id,
                    JobKpiValue.kpi_name == name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                JobKpiValue(
                    job_id=job.id,
                    tool_alias=job.tool_alias,
                    tool_version_id=job.tool_version_id,
                    kpi_name=name,
                    value_numeric=decimal_value,
                    unit=unit,
                    extracted_at=extracted_now,
                )
            )
        else:
            existing.value_numeric = decimal_value
            existing.unit = unit
            existing.extracted_at = extracted_now

    await session.flush()
    await audit.record(
        session,
        RecordInput(
            actor="system:runner",
            actor_type="system",
            action="kpi.extracted",
            resource_type="job",
            resource_id=str(job.id),
            outcome="success",
            payload={
                "tool_alias": job.tool_alias,
                "tool_version_id": job.tool_version_id,
                "extracted": extracted_count,
                "missing": missing_count,
                "total": len(kpi_fields),
            },
        ),
    )
    return KpiExtractResult(extracted=extracted_count, missing=missing_count, total=len(kpi_fields))


# ---- KPI-set signature differ (slice 4 trigger condition) ------------


def compute_kpi_signature(descriptor: Any) -> frozenset[tuple[str, str | None, str | None]]:
    """Compute the per-version KPI signature.

    Returns ``frozenset((name, kpi_label, kpi_unit))`` over the
    descriptor's KPI outputs. Two versions' signatures are equal iff
    re-extracting against either produces the same warehouse columns
    with the same labels and units. ``kpi_category`` is **not** part
    of the signature — it only affects grouping affordances, never
    the captured row.

    Returns the empty frozenset on a descriptor that isn't a dict /
    has no ``outputs`` list / has no KPI fields.

    Used by :func:`should_reextract_after_descriptor_change` (the
    promote-trigger condition) to decide whether a new ToolVersion's
    KPI surface differs enough to warrant a back-fill of jobs on the
    old version.
    """
    if not isinstance(descriptor, dict):
        return frozenset()
    outputs = descriptor.get("outputs", [])
    if not isinstance(outputs, list):
        return frozenset()
    sig: set[tuple[str, str | None, str | None]] = set()
    for f in outputs:
        if not isinstance(f, dict):
            continue
        if f.get("kpi") is not True:
            continue
        name = f.get("name")
        if not isinstance(name, str):
            continue
        label_raw = f.get("kpi_label")
        unit_raw = f.get("kpi_unit") if f.get("kpi_unit") is not None else f.get("unit")
        label = label_raw if isinstance(label_raw, str) else None
        unit = unit_raw if isinstance(unit_raw, str) else None
        sig.add((name, label, unit))
    return frozenset(sig)


def should_reextract_after_descriptor_change(
    *, previous_descriptor: Any, new_descriptor: Any
) -> bool:
    """``True`` when the KPI signature has changed between two descriptors.

    The promote-trigger condition (design §5.4): when a new
    ``ToolVersion`` is created with a descriptor whose KPI set
    (name / label / unit) differs from the immediately-previous
    version's, the back-fill sweep should fire. Equal signatures —
    or two empty signatures — are a no-op.
    """
    return compute_kpi_signature(previous_descriptor) != compute_kpi_signature(new_descriptor)


# ---- Outputs-document loader (slice 4 re-read) -----------------------


def _resolve_outputs_path(*, mount_path: str, working_dir_relative: str) -> Path:
    """Build the absolute ``outputs.json`` path for a completed job.

    The runner writes outputs at ``<wd>/outputs/outputs.json``
    (:class:`OutputsDocument` contract). Re-extraction needs the
    durable copy; the retention floor (ADR-0003) keeps this file
    even after the wider cleanup sweep prunes intermediates.
    """
    return Path(mount_path) / working_dir_relative / "outputs" / "outputs.json"


def load_outputs_document(path: Path) -> OutputsDocument | None:
    """Read + validate an ``outputs.json`` from durable storage.

    Returns ``None`` when the file is missing or malformed — the
    re-extraction sweep treats that as a per-job skip with a
    WARNING log; the audit event records the skip count. Mirrors
    the shape of the runner's ``_read_outputs_document`` (the
    runner version takes a :class:`StagedWorkingDir`; this version
    takes a path because re-extraction has no staging context).
    """
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return OutputsDocument.model_validate(payload)
    except ValueError:
        return None


# ---- Result shape for the sweep --------------------------------------


@dataclass(frozen=True, slots=True)
class KpiReextractResult:
    """Per-tool-version re-extraction sweep tally.

    - :attr:`job_count` — completed jobs of the version walked.
    - :attr:`extracted` — KPI values captured across all jobs.
    - :attr:`missing` — KPI values that landed as ``NULL`` (missing /
      NaN / non-numeric / ±Inf — same classifier as the per-job hook).
    - :attr:`skipped_no_outputs` — jobs whose ``outputs.json`` was
      gone or malformed; no rows written for them.
    """

    job_count: int
    extracted: int
    missing: int
    skipped_no_outputs: int


# ---- Re-extraction worker --------------------------------------------


# Jobs are processed in batches; after each batch we yield the event
# loop so the runner daemon's other loops (heartbeat, pickup, cancel)
# keep ticking. Design §5.4: "rate-limited: yields control every 50
# jobs so the daemon doesn't monopolise the event loop".
_REEXTRACT_YIELD_BATCH_SIZE = 50


async def reextract_tool_version(
    audit: AuditWriter,
    session: AsyncSession,
    *,
    tool_version_id: int,
    triggered_by_user_id: int | None,
    trigger: str = "admin_manual",
    storage_resolver: (Any | None) = None,  # callable(storage_location_id) -> mount_path str | None
    outputs_loader: Any | None = None,  # callable(Path) -> OutputsDocument | None
) -> KpiReextractResult:
    """Re-extract KPI rows for every completed job of a tool version.

    Walks :class:`Job` rows where
    ``tool_version_id == tool_version_id`` and ``state == 'complete'``,
    reads each one's durable ``outputs.json`` via
    :func:`load_outputs_document`, and re-runs
    :func:`extract_kpis_for_job` per job. Jobs whose ``outputs.json``
    is missing / malformed are skipped (counted, audited).

    Serialised per tool_version via :func:`_reextract_state.acquire`:
    if a sweep is already running for the same version, raises
    :class:`KpiReextractInProgressError`. Cross-version sweeps run
    concurrently fine.

    The ``trigger`` field tags the audit event so an operator can
    distinguish admin-initiated back-fills from automatic
    descriptor-promote sweeps. ``triggered_by_user_id`` is ``None``
    for the automatic case.

    ``storage_resolver`` and ``outputs_loader`` are test seams —
    production callers omit them and get the real filesystem
    resolution. The tests inject stubs so the sweep can be exercised
    without writing real outputs.json files at carefully-named paths.
    """
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    async with _reextract_state.acquire(tool_version_id):
        return await _reextract_inner(
            audit,
            session,
            tool_version_id=tool_version_id,
            triggered_by_user_id=triggered_by_user_id,
            trigger=trigger,
            storage_resolver=storage_resolver,
            outputs_loader=outputs_loader,
            _job_cls=Job,
        )


async def _reextract_inner(
    audit: AuditWriter,
    session: AsyncSession,
    *,
    tool_version_id: int,
    triggered_by_user_id: int | None,
    trigger: str,
    storage_resolver: Any | None,
    outputs_loader: Any | None,
    _job_cls: Any,
) -> KpiReextractResult:
    """Inner body of the sweep — split out so the lock contextmanager stays terse."""
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

    loader = outputs_loader if outputs_loader is not None else load_outputs_document

    # Fetch the completed jobs for this version. Ordered by id for a
    # deterministic sweep — the audit narrative reads more cleanly
    # when re-runs of the same sweep walk the same order.
    jobs = list(
        (
            await session.execute(
                select(_job_cls)
                .where(_job_cls.tool_version_id == tool_version_id)
                .where(_job_cls.state == "complete")
                .order_by(_job_cls.id)
            )
        ).scalars()
    )

    # Cache storage_location.mount_path lookups inside the sweep —
    # most jobs in a single tool_version share a storage location.
    if storage_resolver is None:
        mount_path_cache: dict[int, str | None] = {}

        async def _default_resolver(loc_id: int) -> str | None:
            if loc_id in mount_path_cache:
                return mount_path_cache[loc_id]
            loc = await session.get(StorageLocation, loc_id)
            mp = loc.mount_path if loc is not None else None
            mount_path_cache[loc_id] = mp
            return mp

        resolver = _default_resolver
    else:
        resolver = storage_resolver

    extracted_total = 0
    missing_total = 0
    skipped_no_outputs = 0

    for index, job in enumerate(jobs):
        outputs_doc = await _load_for_job(job, resolver=resolver, loader=loader)
        if outputs_doc is None:
            skipped_no_outputs += 1
            _log.warning(
                "KPI re-extract: job=%s outputs.json missing/malformed; skipped",
                job.id,
            )
        else:
            result = await extract_kpis_for_job(audit, session, job=job, outputs_doc=outputs_doc)
            extracted_total += result.extracted
            missing_total += result.missing

        # Rate-limit: yield the event loop every N jobs so the runner
        # daemon's other loops (heartbeat / pickup / cancel) keep
        # ticking smoothly on a populated install.
        if (index + 1) % _REEXTRACT_YIELD_BATCH_SIZE == 0:
            await asyncio.sleep(0)

    await audit.record(
        session,
        RecordInput(
            actor=(
                f"user:{triggered_by_user_id}"
                if triggered_by_user_id is not None
                else "system:runner"
            ),
            actor_type=("user" if triggered_by_user_id is not None else "system"),
            action="kpi.reextracted",
            resource_type="tool_version",
            resource_id=str(tool_version_id),
            outcome="success",
            payload={
                "tool_version_id": tool_version_id,
                "trigger": trigger,
                "job_count": len(jobs),
                "extracted": extracted_total,
                "missing": missing_total,
                "skipped_no_outputs": skipped_no_outputs,
            },
        ),
    )
    return KpiReextractResult(
        job_count=len(jobs),
        extracted=extracted_total,
        missing=missing_total,
        skipped_no_outputs=skipped_no_outputs,
    )


async def _load_for_job(job: Any, *, resolver: Any, loader: Any) -> OutputsDocument | None:
    """Resolve a job's durable outputs path + load the document.

    Returns ``None`` when the job lacks staging metadata or the file
    is missing — the sweep counts that as a skip rather than a hard
    error.
    """
    if job.working_dir_relative is None or job.storage_location_id is None:
        return None
    mount_path = await resolver(job.storage_location_id)
    if mount_path is None:
        return None
    path = _resolve_outputs_path(
        mount_path=mount_path, working_dir_relative=job.working_dir_relative
    )
    return cast("OutputsDocument | None", loader(path))


# ---- Warehouse query shapes (slice 5) --------------------------------


@dataclass(frozen=True, slots=True)
class WarehouseFilters:
    """Query-side filters for the warehouse list + timeseries endpoints.

    All optional; the empty value (``None`` or empty collection) means
    "don't filter on this dimension". ``job_ids`` is the
    unbounded-selection allow-list from the warehouse page (design §8.1
    export bar) — when set, it intersects all other filters and the
    per-user ACL. ``kpi_ranges`` filters on KPI *value*: a job passes a
    ``(kpi_name, min, max)`` entry when it has that KPI within the (open
    on the ``None`` side) range; multiple entries AND together.
    """

    tool_version_id: int | None = None
    project_ids: tuple[int, ...] = ()
    scenario_ids: tuple[int, ...] = ()
    requestor_user_ids: tuple[int, ...] = ()
    completed_from: _dt.datetime | None = None
    completed_to: _dt.datetime | None = None
    job_ids: tuple[int, ...] | None = None
    kpi_ranges: tuple[tuple[str, float | None, float | None], ...] = ()
    """``(kpi_name, min, max)`` value filters; ``min``/``max`` ``None`` =
    open on that side. AND-combined (a job must satisfy every entry)."""
    """When ``None`` no allow-list; when a tuple, restrict to these
    job ids (empty tuple → zero results). Intersected with ACL — IDs
    the user can't see are silently dropped (no info leak)."""

    vv_status: str = "any"
    """ADR-0011 V&V status filter (slice 9). One of:

    - ``"any"`` (default) — no V&V filtering applied.
    - ``"verified_only"`` — restrict to jobs whose ``tool_version``
      carries at least an approved verification record.
    - ``"verified_validated"`` — restrict to jobs whose
      ``tool_version`` carries BOTH approved verification AND
      validation records.

    Resolves through :meth:`matofx.vv.VVService.summarise_tool_status`
    so the cohort-key inheritance (Option C) is honoured: two
    tool_versions sharing a bundle hash share V&V status."""


@dataclass(frozen=True, slots=True)
class ToolKpiCatalogEntry:
    """One KPI declared by a tool — what the warehouse columns look like."""

    kpi_name: str
    kpi_label: str | None
    kpi_unit: str | None
    kpi_category: str | None


@dataclass(frozen=True, slots=True)
class WarehouseRow:
    """One job's identity columns + KPI values for the warehouse table."""

    job_id: int
    project_id: int
    scenario_id: int | None
    parametric_study_id: int | None
    requestor_user_id: int
    completed_at: _dt.datetime | None
    tool_version_id: int
    kpi_values: dict[str, float | None]
    kpi_units: dict[str, str | None]
    vv_status: str = "none"
    """ADR-0011 V&V badge kind for this row's tool_version (slice 9).

    One of ``verified_validated`` / ``verified_only`` / ``pending`` /
    ``none`` / ``retired``. Defaults to ``"none"`` when V&V is
    disabled or the badge couldn't be resolved — the warehouse
    surface degrades to "no badge shown" gracefully."""

    # Human-readable labels for the identity columns (feedback_use_labels_not_names).
    # Resolved alongside the ids so every surface (table / PDF / CSV) can show
    # names rather than bare integers. ``None`` when the referenced row is
    # missing / unresolved — callers fall back to the id.
    project_name: str | None = None
    scenario_name: str | None = None
    requestor_name: str | None = None
    tool_version_label: str | None = None
    """e.g. ``"v1.2.0"`` — the ``ToolVersion.version`` string, ``v``-prefixed."""


@dataclass(frozen=True, slots=True)
class WarehousePage:
    """One page of warehouse rows + the count rollups the UI surfaces."""

    rows: tuple[WarehouseRow, ...]
    page: int
    page_size: int
    total: int
    hidden_by_acl: int


@dataclass(frozen=True, slots=True)
class TimeseriesPoint:
    """One (extracted_at, value, job_id) tuple for the trend chart."""

    extracted_at: _dt.datetime
    value_numeric: float | None
    job_id: int


# ---- Visibility helper -----------------------------------------------


async def _visible_project_ids(session: AsyncSession, actor_user_id: int) -> set[int] | None:
    """Set of project ids the user can READ; ``None`` means "all projects".

    ``None`` is returned for users with ``projects.see_all`` — the
    caller short-circuits the IN-clause entirely (faster than an
    in-clause over every project id in the install).
    """
    from matofx.hierarchy.orm import Project, ProjectAcl  # noqa: PLC0415
    from matofx.users.permissions import Permission, user_has_permission  # noqa: PLC0415

    if await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
        return None
    creator_ids = (
        await session.execute(select(Project.id).where(Project.creator_user_id == actor_user_id))
    ).scalars()
    acl_ids = (
        await session.execute(
            select(ProjectAcl.project_id).where(ProjectAcl.user_id == actor_user_id)
        )
    ).scalars()
    return set(creator_ids) | set(acl_ids)


# ---- KPI catalog (slice 5 §7 — GET /api/tools/{alias}/kpis) ----------


async def list_kpis_for_tool(
    session: AsyncSession,
    *,
    tool_alias: str,
    tool_version_id: int | None = None,
) -> list[ToolKpiCatalogEntry]:
    """List declared KPIs for a tool, deduplicated across versions.

    When ``tool_version_id`` is set, returns the single version's KPIs;
    when ``None``, the union over every released version. The dedup
    key is ``(kpi_name, kpi_label, kpi_unit)`` — two versions that
    declare the same KPI with the same label and unit collapse to one
    catalog entry. A label / unit divergence between versions surfaces
    as separate entries so the warehouse can't accidentally collide
    them into one column.
    """
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    if tool_version_id is not None:
        tv = await session.get(ToolVersion, tool_version_id)
        if tv is None or tv.alias != tool_alias:
            return []
        return _extract_kpi_catalog(tv.descriptor_snapshot)

    # Union across released versions. The Tool row's `state` covers
    # "released" semantically; the ToolVersion table doesn't carry a
    # released flag of its own (versions are immutable snapshots).
    tool = (
        await session.execute(select(Tool).where(Tool.alias == tool_alias))
    ).scalar_one_or_none()
    if tool is None:
        return []
    versions = list(
        (
            await session.execute(select(ToolVersion).where(ToolVersion.alias == tool_alias))
        ).scalars()
    )
    seen: set[tuple[str, str | None, str | None]] = set()
    out: list[ToolKpiCatalogEntry] = []
    for tv in versions:
        for entry in _extract_kpi_catalog(tv.descriptor_snapshot):
            key = (entry.kpi_name, entry.kpi_label, entry.kpi_unit)
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def _extract_kpi_catalog(descriptor: Any) -> list[ToolKpiCatalogEntry]:
    """Pull KPI catalog entries out of a descriptor snapshot dict."""
    if not isinstance(descriptor, dict):
        return []
    outputs = descriptor.get("outputs", [])
    if not isinstance(outputs, list):
        return []
    entries: list[ToolKpiCatalogEntry] = []
    for f in outputs:
        if not isinstance(f, dict) or f.get("kpi") is not True:
            continue
        name = f.get("name")
        if not isinstance(name, str):
            continue
        # Effective KPI label: the explicit ``kpi_label`` override, else the
        # output's own ``label`` — never the internal ``name``
        # ([[feedback_use_labels_not_names]]). Mirrors the kpi_unit fallback below.
        label_raw = f.get("kpi_label") if f.get("kpi_label") is not None else f.get("label")
        label = label_raw if isinstance(label_raw, str) else None
        unit_raw = f.get("kpi_unit") if f.get("kpi_unit") is not None else f.get("unit")
        unit = unit_raw if isinstance(unit_raw, str) else None
        cat_raw = f.get("kpi_category")
        category = cat_raw if isinstance(cat_raw, str) else None
        entries.append(
            ToolKpiCatalogEntry(
                kpi_name=name, kpi_label=label, kpi_unit=unit, kpi_category=category
            )
        )
    return entries


# ---- Warehouse query (slice 5 §7 — GET /api/tools/{alias}/results) ---


_WAREHOUSE_PAGE_SIZE_CAP = 200
_WAREHOUSE_CSV_ROW_CAP = 100_000


def _decimal_to_float(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


async def _build_warehouse_base_stmt(  # noqa: PLR0912 - one branch per case — the branches are the specification
    session: AsyncSession,
    *,
    actor_user_id: int,
    tool_alias: str,
    filters: WarehouseFilters,
) -> Any:
    """Build the visibility-filtered base SELECT for the warehouse query.

    Joins :class:`Job` to its home (scenario OR parametric_study) so
    the visibility filter applies via either. Returns a SELECT over
    Job rows; callers add ORDER BY + LIMIT / OFFSET (or COUNT).
    """
    from matofx.hierarchy.orm import Job, Scenario  # noqa: PLC0415
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    stmt = (
        select(Job)
        .outerjoin(Scenario, Scenario.id == Job.scenario_id)
        .outerjoin(ParametricStudy, ParametricStudy.id == Job.parametric_study_id)
        .where(Job.tool_alias == tool_alias)
        .where(Job.state == "complete")
    )

    visible = await _visible_project_ids(session, actor_user_id)
    if visible is not None:
        from sqlalchemy import or_  # noqa: PLC0415

        if not visible:
            # No accessible projects — short-circuit to an empty result.
            stmt = stmt.where(sa_false())
        else:
            stmt = stmt.where(
                or_(
                    Scenario.project_id.in_(visible),
                    ParametricStudy.project_id.in_(visible),
                )
            )

    if filters.tool_version_id is not None:
        stmt = stmt.where(Job.tool_version_id == filters.tool_version_id)
    if filters.project_ids:
        from sqlalchemy import or_  # noqa: PLC0415

        stmt = stmt.where(
            or_(
                Scenario.project_id.in_(filters.project_ids),
                ParametricStudy.project_id.in_(filters.project_ids),
            )
        )
    if filters.scenario_ids:
        stmt = stmt.where(Job.scenario_id.in_(filters.scenario_ids))
    if filters.requestor_user_ids:
        stmt = stmt.where(Job.created_by_user_id.in_(filters.requestor_user_ids))
    if filters.completed_from is not None:
        stmt = stmt.where(Job.completed_at >= filters.completed_from)
    if filters.completed_to is not None:
        stmt = stmt.where(Job.completed_at <= filters.completed_to)
    if filters.job_ids is not None:
        # Empty tuple → zero results (an explicit "no rows" allow-list).
        if not filters.job_ids:
            stmt = stmt.where(sa_false())
        else:
            stmt = stmt.where(Job.id.in_(filters.job_ids))
    # KPI value ranges: one correlated EXISTS per (kpi, min, max) — the job
    # must have that KPI within range. AND-combined across KPIs.
    if filters.kpi_ranges:
        from sqlalchemy import and_, exists, literal  # noqa: PLC0415

        for kpi_name, vmin, vmax in filters.kpi_ranges:
            conds = [JobKpiValue.job_id == Job.id, JobKpiValue.kpi_name == kpi_name]
            if vmin is not None:
                conds.append(JobKpiValue.value_numeric >= vmin)
            if vmax is not None:
                conds.append(JobKpiValue.value_numeric <= vmax)
            stmt = stmt.where(exists(select(literal(1)).where(and_(*conds))))
    return stmt


def sa_false() -> Any:
    """Portable ``FALSE`` predicate for empty-allow-list short-circuits."""
    from sqlalchemy import literal  # noqa: PLC0415

    return literal(1) == literal(0)


_VV_STATUS_VALUES: frozenset[str] = frozenset({"any", "verified_only", "verified_validated"})


def _vv_status_passes(badge_kind: str, required: str) -> bool:
    """Decide whether a tool_version's badge satisfies the requested filter.

    ``"any"`` → always passes. ``"verified_only"`` → badge must be
    ``verified_only`` or ``verified_validated``. ``"verified_validated"``
    → badge must be exactly ``verified_validated``.
    """
    if required == "any":
        return True
    if required == "verified_only":
        return badge_kind in {"verified_only", "verified_validated"}
    if required == "verified_validated":
        return badge_kind == "verified_validated"
    return True  # unknown filter value → no constraint


async def _resolve_vv_badges(
    session: AsyncSession, *, tool_version_ids: set[int]
) -> dict[int, str]:
    """Batch-resolve V&V badge kinds for distinct ``tool_version_id``.

    Returns ``{tool_version_id: badge_kind}``. Each id is resolved
    via :meth:`VVService.summarise_tool_status` (cohort-key
    inheritance honoured). Failure (V&V tables missing, etc.) is
    silently swallowed — the warehouse degrades to "no badge" when
    V&V isn't usable.
    """
    if not tool_version_ids:
        return {}
    try:
        from matofx.audit import AuditWriter  # noqa: PLC0415
        from matofx.vv import VVService  # noqa: PLC0415

        service = VVService(audit=AuditWriter(signing_key=None, key_id=None))
        result: dict[int, str] = {}
        for tv_id in tool_version_ids:
            try:
                badge = await service.summarise_tool_status(session, tool_version_id=tv_id)
                result[tv_id] = badge.kind
            except Exception:  # noqa: BLE001 — degrade gracefully
                result[tv_id] = "none"
    except ImportError:
        return {}
    else:
        return result


async def _apply_vv_status_filter(
    session: AsyncSession, base_stmt: Any, vv_status: str
) -> Any | None:
    """Narrow a warehouse base SELECT to tool_versions passing the V&V filter.

    Returns the narrowed statement, the statement unchanged when
    ``vv_status == "any"``, or ``None`` when nothing passes (the caller
    should yield an empty result). Shared by the table + CSV paths so both
    honour the filter identically (the V&V join can't live in
    :func:`_build_warehouse_base_stmt` — it resolves through
    :meth:`VVService.summarise_tool_status` for cohort inheritance).
    """
    if vv_status == "any":
        return base_stmt
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    tv_rows = (
        await session.execute(
            select(Job.tool_version_id).distinct().select_from(base_stmt.subquery())
        )
    ).scalars()
    tv_set = {int(tv) for tv in tv_rows if tv is not None}
    badges = await _resolve_vv_badges(session, tool_version_ids=tv_set)
    permitted = {tv for tv, kind in badges.items() if _vv_status_passes(kind, vv_status)}
    if not permitted:
        return None
    return base_stmt.where(Job.tool_version_id.in_(permitted))


async def list_warehouse_rows(
    session: AsyncSession,
    *,
    actor_user_id: int,
    tool_alias: str,
    filters: WarehouseFilters,
    page: int = 1,
    page_size: int = 50,
) -> WarehousePage:
    """Paginated warehouse query — one row per visible completed job + its KPI values.

    Pagination is on the job level (one row per job), so a job with
    N KPIs takes one slot. Newest-completed-first by ``Job.id`` (the
    proxy for ``completed_at`` ordering; using id avoids a NULL-sort
    quirk on the timestamp column).

    The ``hidden_by_acl`` figure counts jobs that match the non-ACL
    filters but are invisible to the caller — surfaces as
    "12 hidden by access controls" in the page footer (design §8.1).
    """
    from sqlalchemy import func  # noqa: PLC0415

    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    page_size = max(1, min(int(page_size), _WAREHOUSE_PAGE_SIZE_CAP))
    page = max(1, int(page))

    # 1. The ACL-filtered visible set.
    base_stmt = await _build_warehouse_base_stmt(
        session,
        actor_user_id=actor_user_id,
        tool_alias=tool_alias,
        filters=filters,
    )

    # 2. V&V status filter (slice 9). Narrow to permitted tool_versions
    # before pagination so the page/total counts honour the constraint.
    narrowed = await _apply_vv_status_filter(session, base_stmt, filters.vv_status)
    if narrowed is None:
        return WarehousePage(rows=(), page=page, page_size=page_size, total=0, hidden_by_acl=0)
    base_stmt = narrowed

    total = (
        await session.execute(select(func.count()).select_from(base_stmt.subquery()))
    ).scalar_one()

    # 2. The ACL-bypassed count — for the "hidden by access controls"
    # delta. Same filter shape, no visibility predicate.
    bypass_stmt = await _build_no_acl_count_stmt(session, tool_alias=tool_alias, filters=filters)
    total_without_acl = (
        await session.execute(select(func.count()).select_from(bypass_stmt.subquery()))
    ).scalar_one()
    hidden_by_acl = max(0, int(total_without_acl) - int(total))

    # 3. The paginated job rows.
    offset = (page - 1) * page_size
    paged = base_stmt.order_by(Job.id.desc()).limit(page_size).offset(offset)
    jobs = list((await session.execute(paged)).scalars())
    if not jobs:
        return WarehousePage(
            rows=(),
            page=page,
            page_size=page_size,
            total=int(total),
            hidden_by_acl=hidden_by_acl,
        )

    # 4. Per-job KPI value pivot.
    job_ids = [j.id for j in jobs]
    kpi_rows = list(
        (
            await session.execute(select(JobKpiValue).where(JobKpiValue.job_id.in_(job_ids)))
        ).scalars()
    )
    kpis_by_job: dict[int, dict[str, float | None]] = {}
    units_by_job: dict[int, dict[str, str | None]] = {}
    for r in kpi_rows:
        kpis_by_job.setdefault(r.job_id, {})[r.kpi_name] = _decimal_to_float(r.value_numeric)
        units_by_job.setdefault(r.job_id, {})[r.kpi_name] = r.unit

    # 5. Resolve each job's owning project (scenario OR study).
    project_id_by_job = await _resolve_project_ids(session, jobs)

    # 6. V&V badge per row — batched per distinct tool_version_id so
    # the populated-install case is O(unique versions), not O(jobs).
    distinct_tv_ids = {j.tool_version_id for j in jobs}
    vv_badges = await _resolve_vv_badges(session, tool_version_ids=distinct_tv_ids)

    # 7. Human-readable labels for the identity columns — batched so the
    # warehouse shows names, not bare ids (feedback_use_labels_not_names).
    labels = await _resolve_identity_labels(
        session,
        project_ids=set(project_id_by_job.values()),
        scenario_ids={j.scenario_id for j in jobs if j.scenario_id is not None},
        user_ids={j.created_by_user_id for j in jobs},
        tool_version_ids=distinct_tv_ids,
    )

    rows = tuple(
        WarehouseRow(
            job_id=j.id,
            project_id=project_id_by_job.get(j.id, 0),
            scenario_id=j.scenario_id,
            parametric_study_id=j.parametric_study_id,
            requestor_user_id=j.created_by_user_id,
            completed_at=j.completed_at,
            tool_version_id=j.tool_version_id,
            kpi_values=kpis_by_job.get(j.id, {}),
            kpi_units=units_by_job.get(j.id, {}),
            vv_status=vv_badges.get(j.tool_version_id, "none"),
            project_name=labels.projects.get(project_id_by_job.get(j.id, 0)),
            scenario_name=(
                labels.scenarios.get(j.scenario_id) if j.scenario_id is not None else None
            ),
            requestor_name=labels.users.get(j.created_by_user_id),
            tool_version_label=labels.tool_versions.get(j.tool_version_id),
        )
        for j in jobs
    )
    return WarehousePage(
        rows=rows,
        page=page,
        page_size=page_size,
        total=int(total),
        hidden_by_acl=hidden_by_acl,
    )


async def _build_no_acl_count_stmt(
    session: AsyncSession,  # noqa: ARG001 - signature kept uniform across the sibling callers
    *,
    tool_alias: str,
    filters: WarehouseFilters,
) -> Any:
    """Same shape as ``_build_warehouse_base_stmt`` minus the ACL filter."""
    from matofx.hierarchy.orm import Job, Scenario  # noqa: PLC0415
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    stmt = (
        select(Job)
        .outerjoin(Scenario, Scenario.id == Job.scenario_id)
        .outerjoin(ParametricStudy, ParametricStudy.id == Job.parametric_study_id)
        .where(Job.tool_alias == tool_alias)
        .where(Job.state == "complete")
    )
    if filters.tool_version_id is not None:
        stmt = stmt.where(Job.tool_version_id == filters.tool_version_id)
    if filters.project_ids:
        from sqlalchemy import or_  # noqa: PLC0415

        stmt = stmt.where(
            or_(
                Scenario.project_id.in_(filters.project_ids),
                ParametricStudy.project_id.in_(filters.project_ids),
            )
        )
    if filters.scenario_ids:
        stmt = stmt.where(Job.scenario_id.in_(filters.scenario_ids))
    if filters.requestor_user_ids:
        stmt = stmt.where(Job.created_by_user_id.in_(filters.requestor_user_ids))
    if filters.completed_from is not None:
        stmt = stmt.where(Job.completed_at >= filters.completed_from)
    if filters.completed_to is not None:
        stmt = stmt.where(Job.completed_at <= filters.completed_to)
    if filters.job_ids is not None:
        if not filters.job_ids:
            stmt = stmt.where(sa_false())
        else:
            stmt = stmt.where(Job.id.in_(filters.job_ids))
    return stmt


async def _resolve_project_ids(session: AsyncSession, jobs: list[Any]) -> dict[int, int]:
    """Map ``job_id → project_id`` via scenario or parametric_study."""
    from matofx.hierarchy.orm import Scenario  # noqa: PLC0415
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    scenario_ids = {j.scenario_id for j in jobs if j.scenario_id is not None}
    study_ids = {j.parametric_study_id for j in jobs if j.parametric_study_id is not None}

    scen_map: dict[int, int] = {}
    if scenario_ids:
        rows = list(
            (
                await session.execute(
                    select(Scenario.id, Scenario.project_id).where(Scenario.id.in_(scenario_ids))
                )
            ).all()
        )
        # Not ``dict(rows)``: SQLAlchemy's Row is a tuple at runtime but
        # mypy does not accept it as one, and the comprehension types.
        scen_map = {sid: pid for sid, pid in rows}  # noqa: C416

    study_map: dict[int, int] = {}
    if study_ids:
        rows = list(
            (
                await session.execute(
                    select(ParametricStudy.id, ParametricStudy.project_id).where(
                        ParametricStudy.id.in_(study_ids)
                    )
                )
            ).all()
        )
        # Not ``dict(rows)``: SQLAlchemy's Row is a tuple at runtime but
        # mypy does not accept it as one, and the comprehension types.
        study_map = {sid: pid for sid, pid in rows}  # noqa: C416

    out: dict[int, int] = {}
    for j in jobs:
        if j.scenario_id is not None and j.scenario_id in scen_map:
            out[j.id] = scen_map[j.scenario_id]
        elif j.parametric_study_id is not None and j.parametric_study_id in study_map:
            out[j.id] = study_map[j.parametric_study_id]
    return out


@dataclass(frozen=True, slots=True)
class _IdentityLabels:
    """Batched id → display-name maps for the warehouse identity columns."""

    projects: dict[int, str]
    scenarios: dict[int, str]
    users: dict[int, str]
    tool_versions: dict[int, str]


async def _resolve_identity_labels(
    session: AsyncSession,
    *,
    project_ids: set[int],
    scenario_ids: set[int],
    user_ids: set[int],
    tool_version_ids: set[int],
) -> _IdentityLabels:
    """Batch-resolve names for the identity columns (one query per dimension).

    Keeps the warehouse showing labels rather than bare ids
    (feedback_use_labels_not_names). Each map omits ids that don't
    resolve, so the caller falls back to the raw id for orphans.
    """
    from matofx.hierarchy.orm import Project, Scenario  # noqa: PLC0415
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415
    from matofx.users.orm import User  # noqa: PLC0415

    projects: dict[int, str] = {}
    real_project_ids = {p for p in project_ids if p}
    if real_project_ids:
        for pid, name in (
            await session.execute(
                select(Project.id, Project.name).where(Project.id.in_(real_project_ids))
            )
        ).all():
            projects[pid] = name

    scenarios: dict[int, str] = {}
    if scenario_ids:
        for sid, name in (
            await session.execute(
                select(Scenario.id, Scenario.name).where(Scenario.id.in_(scenario_ids))
            )
        ).all():
            scenarios[sid] = name

    users: dict[int, str] = {}
    if user_ids:
        for uid, full_name, username in (
            await session.execute(
                select(User.id, User.full_name, User.username).where(User.id.in_(user_ids))
            )
        ).all():
            users[uid] = full_name or username

    tool_versions: dict[int, str] = {}
    if tool_version_ids:
        for tvid, version in (
            await session.execute(
                select(ToolVersion.id, ToolVersion.version).where(
                    ToolVersion.id.in_(tool_version_ids)
                )
            )
        ).all():
            tool_versions[tvid] = f"v{version}"

    return _IdentityLabels(
        projects=projects,
        scenarios=scenarios,
        users=users,
        tool_versions=tool_versions,
    )


# ---- Timeseries (slice 5 §7 — GET /results/timeseries) ---------------


async def timeseries_for_kpi(
    session: AsyncSession,
    *,
    actor_user_id: int,
    tool_alias: str,
    kpi_name: str,
    filters: WarehouseFilters,
) -> list[TimeseriesPoint]:
    """Return ``(extracted_at, value, job_id)`` points for one KPI.

    Same ACL + filter composition as :func:`list_warehouse_rows`, plus
    a final JOIN to ``job_kpi_value`` to pull the KPI's row per job.
    Ordered by ``extracted_at`` ascending so the plot consumer can
    just render in order.
    """
    base_stmt = await _build_warehouse_base_stmt(
        session,
        actor_user_id=actor_user_id,
        tool_alias=tool_alias,
        filters=filters,
    )
    job_rows = list((await session.execute(base_stmt)).scalars())
    if not job_rows:
        return []
    job_ids = [j.id for j in job_rows]
    kpi_rows = list(
        (
            await session.execute(
                select(JobKpiValue)
                .where(JobKpiValue.job_id.in_(job_ids))
                .where(JobKpiValue.kpi_name == kpi_name)
                .order_by(JobKpiValue.extracted_at.asc())
            )
        ).scalars()
    )
    return [
        TimeseriesPoint(
            extracted_at=r.extracted_at,
            value_numeric=_decimal_to_float(r.value_numeric),
            job_id=r.job_id,
        )
        for r in kpi_rows
    ]


# ---- CSV export iterator (slice 5 §7 — GET /results/export.csv) ------


def _csv_kpi_header(entry: ToolKpiCatalogEntry) -> str:
    """A KPI column header: its label (never the internal name), with unit."""
    label = entry.kpi_label or entry.kpi_name
    return f"{label} ({entry.kpi_unit})" if entry.kpi_unit else label


async def iter_warehouse_csv(
    session: AsyncSession,
    *,
    actor_user_id: int,
    tool_alias: str,
    filters: WarehouseFilters,
    kpi_catalog: list[ToolKpiCatalogEntry],
) -> AsyncIterator[str]:
    """Yield CSV lines for the warehouse query.

    Capped at :data:`_WAREHOUSE_CSV_ROW_CAP` rows; a final marker
    line (``# truncated``) appears when the cap is hit. Headers are
    human labels — the identity columns read "Job ID", "Project",
    "Requestor", … and each KPI column is its label (+ unit), never the
    internal ``kpi_name`` ([[feedback_use_labels_not_names]]). Decimal
    values are written via ``Decimal.__str__`` directly (no float
    round-trip) — matches the audit-truth design (§15).
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    # KPI column header = the KPI's label (+ its unit when set), never the
    # internal kpi_name — [[feedback_use_labels_not_names]]. Identity columns
    # get human headers too; id columns are kept (useful for a data export)
    # but clearly labelled "… ID".
    kpi_columns = [(e.kpi_name, _csv_kpi_header(e)) for e in kpi_catalog]
    header = [
        "Job ID",
        "Project ID",
        "Project",
        "Scenario ID",
        "Scenario",
        "Parametric study ID",
        "Requestor ID",
        "Requestor",
        "Completed",
        "Tool version ID",
        "Tool version",
    ] + [label for _name, label in kpi_columns]

    def _line(fields: list[str]) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerow(fields)
        return buf.getvalue()

    yield _line(header)

    base_stmt = await _build_warehouse_base_stmt(
        session,
        actor_user_id=actor_user_id,
        tool_alias=tool_alias,
        filters=filters,
    )
    # Honour the V&V-status filter (same helper the table uses) so the CSV
    # matches the filtered view; ``None`` means nothing passes (header only,
    # which was already emitted above).
    narrowed = await _apply_vv_status_filter(session, base_stmt, filters.vv_status)
    if narrowed is None:
        return
    base_stmt = narrowed
    capped_stmt = base_stmt.order_by(Job.id.desc()).limit(_WAREHOUSE_CSV_ROW_CAP + 1)
    jobs = list((await session.execute(capped_stmt)).scalars())
    truncated = len(jobs) > _WAREHOUSE_CSV_ROW_CAP
    if truncated:
        jobs = jobs[:_WAREHOUSE_CSV_ROW_CAP]

    if jobs:
        job_ids = [j.id for j in jobs]
        kpi_rows = list(
            (
                await session.execute(select(JobKpiValue).where(JobKpiValue.job_id.in_(job_ids)))
            ).scalars()
        )
        per_job: dict[int, dict[str, Decimal | None]] = {}
        for r in kpi_rows:
            per_job.setdefault(r.job_id, {})[r.kpi_name] = r.value_numeric

        project_id_by_job = await _resolve_project_ids(session, jobs)
        labels = await _resolve_identity_labels(
            session,
            project_ids=set(project_id_by_job.values()),
            scenario_ids={j.scenario_id for j in jobs if j.scenario_id is not None},
            user_ids={j.created_by_user_id for j in jobs},
            tool_version_ids={j.tool_version_id for j in jobs},
        )

        for j in jobs:
            project_id = project_id_by_job.get(j.id)
            cells = [
                str(j.id),
                str(project_id) if project_id is not None else "",
                labels.projects.get(project_id, "") if project_id else "",
                str(j.scenario_id) if j.scenario_id is not None else "",
                labels.scenarios.get(j.scenario_id, "") if j.scenario_id is not None else "",
                str(j.parametric_study_id) if j.parametric_study_id is not None else "",
                str(j.created_by_user_id),
                labels.users.get(j.created_by_user_id, ""),
                j.completed_at.isoformat() if j.completed_at is not None else "",
                str(j.tool_version_id),
                labels.tool_versions.get(j.tool_version_id, ""),
            ]
            row_kpis = per_job.get(j.id, {})
            for kpi_name, _label in kpi_columns:
                v = row_kpis.get(kpi_name)
                # Decimal.__str__ — no Float round-trip.
                cells.append("" if v is None else str(v))
            yield _line(cells)

    if truncated:
        yield "# truncated: result set exceeded 100000 rows\n"
