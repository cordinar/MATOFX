"""Approvals router — the approver inbox + batch decisions (§16.15).

GET  /api/users/me/approvals          → pending requests for the caller
                                        (admins get the superset).
GET  /api/users/me/approvals/summary  → {can_approve, pending} for the
                                        home card + nav gate.
GET  /api/approvals/{id}              → the rich review payload for one
                                        request (identity, footprint,
                                        grouped inputs, justification).
POST /api/approvals/decisions         → batch approve/deny; each item is
                                        applied in its own savepoint so
                                        one bad item doesn't sink the
                                        rest.

Authorisation is per-request, not endpoint-wide: the service classifies
``approver`` / ``self`` / ``admin_override`` and refuses anyone who
isn't an eligible approver (or admin). The list/detail surfaces mirror
that — a non-eligible non-admin caller simply sees nothing / a 404.
"""

from __future__ import annotations

import logging
import statistics
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_approval_service,
    get_current_user,
    get_db,
    get_job_service,
    get_library_service,
    get_materials_service,
    get_model_service,
    get_notifier,
    get_optimisation_service,
    get_study_service,
    get_vv_service,
)
from matofx.api.schemas import (
    ApprovalConstraint,
    ApprovalDecisionResult,
    ApprovalDecisionsRequest,
    ApprovalDecisionsResponse,
    ApprovalDetail,
    ApprovalFootprint,
    ApprovalInputGroup,
    ApprovalInputRow,
    ApprovalLicence,
    ApprovalListItem,
    ApprovalObjective,
    ApprovalOptimisationReview,
    ApprovalResultGroup,
    ApprovalResultRow,
    ApprovalRunAggregate,
    ApprovalRunColumn,
    ApprovalRunMatrix,
    ApprovalRunRow,
    ApprovalStopping,
    ApprovalStrategy,
    ApprovalSummary,
    ApprovalSurrogateMetric,
    ApprovalSurrogateReview,
    ApprovalVariable,
)
from matofx.approvals import ApprovalError, ApprovalService
from matofx.approvals.orm import ApprovalRequest
from matofx.hierarchy.errors import HierarchyError, NotFoundError
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.hierarchy.service import JobService
from matofx.library import LibraryError, LibraryService
from matofx.library.orm import LibraryFile, LibraryFileVersion
from matofx.materials import MaterialsError, MaterialsService
from matofx.materials.orm import Material, MaterialClass, MaterialProperty, MaterialVersion
from matofx.models import ModelError, ModelService
from matofx.models.orm import SurrogateModel, SurrogateModelVersion
from matofx.notify import ApprovalDecisionNotice, Notifier
from matofx.studies import StudyGenerationError, StudyService, StudyStateError
from matofx.studies.optimisation import OptimisationService
from matofx.studies.optimisation.errors import OptimisationError
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.summaries.collectors._descriptor import DescriptorMaps, descriptor_maps
from matofx.tools.orm import Tool, ToolVersion
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.vv import JobVVRecord, ToolVVRecord, VVError, VVService

_log = logging.getLogger(__name__)

router = APIRouter()

# Human labels for optimisation strategy codes + run modes + surrogate
# fitting methods — the approver review never shows the internal code
# ([[feedback_use_labels_not_names]]). Unknown codes degrade to the code.
_STRATEGY_LABELS: dict[str, str] = {
    "bo": "Bayesian optimisation",
    "cmaes": "CMA-ES",
    "de": "Differential evolution",
    "pso": "Particle swarm",
    "ga": "Genetic algorithm",
    "nsga2": "NSGA-II",
    "nsga3": "NSGA-III",
    "moea_d": "MOEA/D",
    "random": "Random search",
    "grid": "Grid search",
}
_RUN_MODE_LABELS: dict[str, str] = {
    "real": "Real evaluations",
    "surrogate": "Surrogate predictions",
    "surrogate_assisted_real": "Surrogate-assisted real",
}
_ACQUISITION_LABELS: dict[str, str] = {
    "optimise": "Exploit (optimise)",
    "balanced": "Balanced",
    "explore": "Explore",
}
_CONVERGENCE_LABELS: dict[str, str] = {
    "best_so_far": "best-so-far improvement",
    "hypervolume": "hypervolume",
}
_SURROGATE_METHOD_LABELS: dict[str, str] = {
    "gp": "Gaussian process",
    "gp_matern": "Gaussian process (Matérn)",
    "poly": "Polynomial response surface",
    "gbm": "Gradient-boosted trees",
}


async def _is_admin(db: AsyncSession, user: User) -> bool:
    """See-all capability — the approvals 'admin' notion (§16.15)."""
    return await user_has_permission(db, user.id, Permission.PROJECTS_SEE_ALL)


async def _can_act_on(
    db: AsyncSession, request: ApprovalRequest, user: User, *, is_admin: bool
) -> bool:
    """Can ``user`` see / decide ``request``? Kind-aware (mirrors the decide path).

    Admins always can. Otherwise the user must be in the request's
    snapshotted ``eligible_approvers`` **and** still hold the authority
    for the gate's kind: the tool-approver flag for job / study gates, or
    the ``LIBRARY_CURATE`` / ``MATERIALS_CURATE`` capability for library /
    material gates (which have no tool-approver pool, ADR-0006/0007).
    """
    if is_admin:
        return True
    if (user.username or "").strip().lower() not in (request.eligible_approvers or []):
        return False
    # Capability gates (no tool-approver pool) require the gate-kind's
    # curate permission; everything else falls back to the approver flag.
    capability_perm: Permission | None = None
    if request.library_file_version_id is not None:
        capability_perm = Permission.LIBRARY_CURATE
    elif request.material_version_id is not None:
        capability_perm = Permission.MATERIALS_CURATE
    elif request.tool_vv_record_id is not None or request.job_vv_record_id is not None:
        capability_perm = Permission.VV_CURATE
    elif request.surrogate_model_version_id is not None:
        capability_perm = Permission.MODELS_CURATE
    elif request.optimisation_study_id is not None and request.kind == "pre":
        # The start gate is the directed compute-cost sign-off — its
        # authority is OPTIMISATION_APPROVE, not the tool-approver flag.
        # The results-release (post) gate uses the tool approver pool, so
        # it falls through to ``is_approver`` like every other post gate.
        capability_perm = Permission.OPTIMISATION_APPROVE
    if capability_perm is not None:
        return await user_has_permission(db, user.id, capability_perm)
    return bool(user.is_approver)


async def _enrich(  # noqa: PLR0915 — a flat batch-resolver: one fetch + one branch per target kind.
    db: AsyncSession, requests: list[ApprovalRequest]
) -> list[ApprovalListItem]:
    """Batch-resolve job/scenario/project/tool/requestor labels."""
    if not requests:
        return []
    job_ids = {r.job_id for r in requests if r.job_id is not None}
    jobs = {j.id: j for j in (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()}
    study_ids = {r.parametric_study_id for r in requests if r.parametric_study_id is not None}
    studies = {
        s.id: s
        for s in (
            await db.execute(select(ParametricStudy).where(ParametricStudy.id.in_(study_ids)))
        ).scalars()
    }
    opt_ids = {r.optimisation_study_id for r in requests if r.optimisation_study_id is not None}
    opt_studies = {
        s.id: s
        for s in (
            await db.execute(select(OptimisationStudy).where(OptimisationStudy.id.in_(opt_ids)))
        ).scalars()
    }
    # Surrogate publish gates (ADR-0008): version → owning model for name + tool.
    sm_version_ids = {
        r.surrogate_model_version_id for r in requests if r.surrogate_model_version_id is not None
    }
    sm_versions = {
        v.id: v
        for v in (
            await db.execute(
                select(SurrogateModelVersion).where(SurrogateModelVersion.id.in_(sm_version_ids))
            )
        ).scalars()
    }
    sm_models = {
        m.id: m
        for m in (
            await db.execute(
                select(SurrogateModel).where(
                    SurrogateModel.id.in_({v.surrogate_model_id for v in sm_versions.values()})
                )
            )
        ).scalars()
    }
    scenario_ids = {j.scenario_id for j in jobs.values() if j.scenario_id is not None}
    scenarios = {
        s.id: s
        for s in (await db.execute(select(Scenario).where(Scenario.id.in_(scenario_ids)))).scalars()
    }
    project_ids = (
        {s.project_id for s in scenarios.values()}
        | {st.project_id for st in studies.values()}
        | {os.project_id for os in opt_studies.values()}
    )
    projects = {
        p.id: p
        for p in (await db.execute(select(Project).where(Project.id.in_(project_ids)))).scalars()
    }
    # Optimisation / surrogate gates carry no denormalised tool_alias on the
    # request — resolve the tool name through the study / model instead.
    aliases = {r.tool_alias for r in requests if r.tool_alias is not None}
    aliases |= {os.tool_alias for os in opt_studies.values() if os.tool_alias}
    aliases |= {m.tool_alias for m in sm_models.values() if m.tool_alias}
    tools = {
        t.alias: t
        for t in (await db.execute(select(Tool).where(Tool.alias.in_(aliases)))).scalars()
    }
    requestor_ids = {r.requested_by_user_id for r in requests}
    users = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(requestor_ids)))).scalars()
    }
    # Tool-less targets (ADR-0006/0007/0011): resolve the library-file /
    # material / V&V-record name through the gated row.
    lib_names = await _library_target_names(db, requests)
    mat_names = await _material_target_names(db, requests)
    tool_vv_names = await _tool_vv_target_names(db, requests)
    job_vv_names = await _job_vv_target_names(db, requests)

    out: list[ApprovalListItem] = []
    for r in requests:
        tool = tools.get(r.tool_alias) if r.tool_alias is not None else None
        tool_name = tool.name if tool is not None else r.tool_alias
        requestor = _display_name(users.get(r.requested_by_user_id))
        common = {
            "request_id": r.id,
            "kind": r.kind,
            "requestor_name": requestor,
            "requested_at": r.requested_at,
            "justification": r.justification or "",
        }
        if r.parametric_study_id is not None:
            study = studies.get(r.parametric_study_id)
            project = projects.get(study.project_id) if study is not None else None
            out.append(
                ApprovalListItem(
                    target_kind="study",
                    study_id=r.parametric_study_id,
                    job_name=study.name if study is not None else f"Study {r.parametric_study_id}",
                    tool_alias=r.tool_alias,
                    tool_name=tool_name,
                    project_name=project.name if project is not None else "",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        elif r.library_file_version_id is not None:
            out.append(
                ApprovalListItem(
                    target_kind="library",
                    job_name=lib_names.get(r.library_file_version_id, "Library file"),
                    project_name="",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        elif r.material_version_id is not None:
            out.append(
                ApprovalListItem(
                    target_kind="material",
                    job_name=mat_names.get(r.material_version_id, "Material"),
                    project_name="",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        elif r.tool_vv_record_id is not None:
            out.append(
                ApprovalListItem(
                    target_kind="vv_tool",
                    job_name=tool_vv_names.get(r.tool_vv_record_id, "V&V record"),
                    project_name="",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        elif r.job_vv_record_id is not None:
            out.append(
                ApprovalListItem(
                    target_kind="vv_job",
                    job_name=job_vv_names.get(r.job_vv_record_id, "V&V record"),
                    project_name="",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        elif r.optimisation_study_id is not None:
            opt = opt_studies.get(r.optimisation_study_id)
            project = projects.get(opt.project_id) if opt is not None else None
            opt_tool = tools.get(opt.tool_alias) if opt is not None and opt.tool_alias else None
            out.append(
                ApprovalListItem(
                    target_kind="optimisation",
                    optimisation_study_id=r.optimisation_study_id,
                    job_name=(
                        opt.name
                        if opt is not None
                        else f"Optimisation study {r.optimisation_study_id}"
                    ),
                    tool_alias=opt.tool_alias if opt is not None else None,
                    tool_name=(
                        opt_tool.name
                        if opt_tool is not None
                        else (opt.tool_alias if opt is not None else None)
                    ),
                    project_name=project.name if project is not None else "",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        elif r.surrogate_model_version_id is not None:
            version = sm_versions.get(r.surrogate_model_version_id)
            model = sm_models.get(version.surrogate_model_id) if version is not None else None
            sm_tool = (
                tools.get(model.tool_alias) if model is not None and model.tool_alias else None
            )
            name = (
                f"{model.name} (v{version.ordinal})"
                if model is not None and version is not None
                else "Surrogate model"
            )
            out.append(
                ApprovalListItem(
                    target_kind="surrogate_model",
                    job_name=name,
                    tool_alias=model.tool_alias if model is not None else None,
                    tool_name=(sm_tool.name if sm_tool is not None else None),
                    project_name="",
                    scenario_name="",
                    **common,  # type: ignore[arg-type]
                )
            )
        else:
            job = jobs.get(r.job_id) if r.job_id is not None else None
            scenario = (
                scenarios.get(job.scenario_id)
                if job is not None and job.scenario_id is not None
                else None
            )
            project = projects.get(scenario.project_id) if scenario is not None else None
            out.append(
                ApprovalListItem(
                    target_kind="job",
                    job_id=r.job_id,
                    job_name=job.name if job is not None else f"Job {r.job_id}",
                    tool_alias=r.tool_alias,
                    tool_name=tool_name,
                    project_name=project.name if project is not None else "",
                    scenario_name=scenario.name if scenario is not None else "",
                    **common,  # type: ignore[arg-type]
                )
            )
    return out


async def _library_target_names(
    db: AsyncSession, requests: list[ApprovalRequest]
) -> dict[int, str]:
    """``{library_file_version_id: file name}`` for the library gates in ``requests``."""
    version_ids = {r.library_file_version_id for r in requests if r.library_file_version_id}
    if not version_ids:
        return {}
    versions = {
        v.id: v
        for v in (
            await db.execute(
                select(LibraryFileVersion).where(LibraryFileVersion.id.in_(version_ids))
            )
        ).scalars()
    }
    file_ids = {v.library_file_id for v in versions.values()}
    files = {
        f.id: f
        for f in (
            await db.execute(select(LibraryFile).where(LibraryFile.id.in_(file_ids)))
        ).scalars()
    }
    out: dict[int, str] = {}
    for vid, v in versions.items():
        f = files.get(v.library_file_id)
        out[vid] = f"{f.name} (v{v.ordinal})" if f is not None else f"Library file version {vid}"
    return out


async def _material_target_names(
    db: AsyncSession, requests: list[ApprovalRequest]
) -> dict[int, str]:
    """``{material_version_id: material name}`` for the material gates in ``requests``."""
    version_ids = {r.material_version_id for r in requests if r.material_version_id}
    if not version_ids:
        return {}
    versions = {
        v.id: v
        for v in (
            await db.execute(select(MaterialVersion).where(MaterialVersion.id.in_(version_ids)))
        ).scalars()
    }
    material_ids = {v.material_id for v in versions.values()}
    materials = {
        m.id: m
        for m in (await db.execute(select(Material).where(Material.id.in_(material_ids)))).scalars()
    }
    out: dict[int, str] = {}
    for vid, v in versions.items():
        m = materials.get(v.material_id)
        out[vid] = f"{m.name} (v{v.ordinal})" if m is not None else f"Material version {vid}"
    return out


async def _tool_vv_target_names(
    db: AsyncSession, requests: list[ApprovalRequest]
) -> dict[int, str]:
    """``{tool_vv_record_id: human-readable label}`` for tool-V&V gates in ``requests``.

    Resolves through the V&V record's ``tool_version_id`` to the tool's
    alias + version string. ADR-0011: V&V records are about a tool
    version, so the inbox row labels accordingly ("V&V on ``heat_transfer
    1.2.0``").
    """
    record_ids = {r.tool_vv_record_id for r in requests if r.tool_vv_record_id}
    if not record_ids:
        return {}
    records = {
        v.id: v
        for v in (
            await db.execute(select(ToolVVRecord).where(ToolVVRecord.id.in_(record_ids)))
        ).scalars()
    }
    tool_version_ids = {r.tool_version_id for r in records.values()}
    tool_versions = {
        tv.id: tv
        for tv in (
            await db.execute(select(ToolVersion).where(ToolVersion.id.in_(tool_version_ids)))
        ).scalars()
    }
    out: dict[int, str] = {}
    for rid, rec in records.items():
        tv = tool_versions.get(rec.tool_version_id)
        if tv is None:
            out[rid] = f"V&V record {rid}"
            continue
        out[rid] = f"{rec.kind.capitalize()} on {tv.alias} {tv.version}"
    return out


async def _job_vv_target_names(db: AsyncSession, requests: list[ApprovalRequest]) -> dict[int, str]:
    """``{job_vv_record_id: human-readable label}`` for job-V&V gates in ``requests``."""
    record_ids = {r.job_vv_record_id for r in requests if r.job_vv_record_id}
    if not record_ids:
        return {}
    records = {
        v.id: v
        for v in (
            await db.execute(select(JobVVRecord).where(JobVVRecord.id.in_(record_ids)))
        ).scalars()
    }
    job_ids = {r.job_id for r in records.values()}
    jobs = {j.id: j for j in (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()}
    out: dict[int, str] = {}
    for rid, rec in records.items():
        job = jobs.get(rec.job_id)
        if job is None:
            out[rid] = f"V&V record {rid}"
            continue
        out[rid] = f"{rec.kind.replace('_', ' ').capitalize()} on {job.name}"
    return out


def _display_name(user: User | None) -> str:
    if user is None:
        return "(unknown)"
    full = (user.full_name or "").strip()
    return full or user.username or "(unknown)"


def _notice(
    target_id: int, target_name: str, request: ApprovalRequest, decision: str
) -> tuple[int, str, str, int, str, str, str | None]:
    """Build a decided-gate tuple for the requestor notification (job / study)."""
    return (
        target_id,
        target_name,
        request.tool_alias or "",
        request.requested_by_user_id,
        request.kind,
        decision,
        request.decision_reason if decision == "deny" else None,
    )


@router.get("/users/me/approvals", response_model=list[ApprovalListItem])
async def list_my_approvals(
    db: Annotated[AsyncSession, Depends(get_db)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ApprovalListItem]:
    """Pending requests waiting on the caller, oldest first.

    Admins get the **superset** (every pending request) for oversight;
    everyone else gets only the requests whose snapshotted
    ``eligible_approvers`` lists them (and only while their approver
    flag is on).
    """
    is_admin = await _is_admin(db, current_user)
    if is_admin:
        requests = await approval_service.list_pending_for_user(
            db, username=current_user.username or "", include_all=True
        )
    else:
        # Candidates = gates whose snapshot lists this user; then keep
        # only those they still hold authority over (tool-approver flag
        # for job/study, curate capability for library/material gates).
        candidates = await approval_service.list_pending_for_user(
            db, username=current_user.username or "", include_all=False
        )
        requests = [r for r in candidates if await _can_act_on(db, r, current_user, is_admin=False)]
    return await _enrich(db, requests)


@router.get("/users/me/approvals/summary", response_model=ApprovalSummary)
async def my_approvals_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ApprovalSummary:
    """Drive the home card + nav gate.

    ``can_approve`` is legacy parity: the card shows whenever the user
    carries the approver flag (then "No pending approvals" when the
    count is zero). ``pending`` is the *personal* count — requests
    addressed to this user — even for admins, so the badge reads
    "waiting on you" rather than the whole-install backlog.
    """
    can_approve = bool(current_user.is_approver)
    pending = 0
    if can_approve:
        pending = await approval_service.count_pending_for_user(
            db, username=current_user.username or "", include_all=False
        )
    return ApprovalSummary(can_approve=can_approve, pending=pending)


@router.get("/approvals/{request_id}", response_model=ApprovalDetail)
async def get_approval_detail(
    request_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ApprovalDetail:
    """The rich review panel for one request.

    Existence-hidden (404) from anyone who isn't an eligible approver
    or an admin — same gate the decision endpoint enforces, so the
    detail surface never leaks a job's inputs to a non-approver.

    For a ``post`` request the panel also carries the run's produced
    results (``result_groups``): the approver is deciding whether to
    *release* those results, so they must be able to see them. The
    eligibility gate above is the same authority the §16.15 output
    lockdown grants over a ``pending_results`` job, so reading the
    output document here is consistent — not a leak.
    """
    request = await approval_service.get(db, request_id=request_id)
    is_admin = await _is_admin(db, current_user)
    if not await _can_act_on(db, request, current_user, is_admin=is_admin):
        # Hide existence rather than 403 — don't confirm the request
        # (and its job) to someone with no business seeing it.
        raise NotFoundError("approval request", request_id)

    # Specialised (tool-less / study / optimisation / surrogate) gates each
    # own a handler; exactly one target column is set (XOR CHECK). Dispatch
    # by the first non-null target, falling through to the job-gate path.
    specialised = (
        (request.parametric_study_id, _study_approval_detail),
        (request.library_file_version_id, _library_approval_detail),
        (request.material_version_id, _material_approval_detail),
        (request.tool_vv_record_id, _tool_vv_approval_detail),
        (request.job_vv_record_id, _job_vv_approval_detail),
        (request.optimisation_study_id, _optimisation_approval_detail),
        (request.surrogate_model_version_id, _surrogate_approval_detail),
    )
    for target_id, handler in specialised:
        if target_id is not None:
            return await handler(db, request)

    if request.job_id is None:  # XOR invariant — defensive narrowing
        raise NotFoundError("approval request", request_id)
    job = await db.get(Job, request.job_id)
    if job is None:
        raise NotFoundError("job", request.job_id)
    scenario = await db.get(Scenario, job.scenario_id) if job.scenario_id is not None else None
    project = await db.get(Project, scenario.project_id) if scenario is not None else None
    tool = (
        await db.execute(select(Tool).where(Tool.alias == request.tool_alias))
    ).scalar_one_or_none()
    requestor = await db.get(User, request.requested_by_user_id)

    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    tool_version = await db.get(ToolVersion, request.tool_version_id)
    snapshot = (
        dict(tool_version.descriptor_snapshot)
        if tool_version is not None and tool_version.descriptor_snapshot
        else {}
    )
    cpu, ram, gpu, licences, groups = _build_review(snapshot, dict(job.inputs or {}))

    # Post-approval: surface what the run produced so the approver can
    # decide release/withhold. Best-effort — a momentarily unreadable
    # outputs document must not blank the whole review panel.
    result_groups: list[ApprovalResultGroup] = []
    if request.kind == "post":
        try:
            outputs_doc = await job_service.get_output_document(db, job_id=job.id)
            result_groups = _build_results(snapshot, outputs_doc.model_dump(mode="json"))
        except HierarchyError:
            result_groups = []

    return ApprovalDetail(
        request_id=request.id,
        kind=request.kind,  # type: ignore[arg-type]
        status=request.status,
        job_id=job.id,
        job_name=job.name,
        tool_alias=request.tool_alias,
        tool_name=tool.name if tool is not None else request.tool_alias,
        tool_version=str(snapshot.get("version") or (tool.version if tool else "")),
        project_name=project.name if project is not None else "",
        scenario_name=scenario.name if scenario is not None else "",
        requestor_name=_display_name(requestor),
        requested_at=request.requested_at,
        justification=request.justification or "",
        cpu_cores=cpu,
        ram_mb=ram,
        gpu_memory_mb=gpu,
        licences=licences,
        input_groups=groups,
        result_groups=result_groups,
    )


def _study_vary_domain(  # noqa: PLR0911 — a flat dispatch: one return per vary kind.
    vary: dict[str, Any] | None, ftype: Any
) -> tuple[float | None, float | None, list[Any], Any]:
    """A factor's variation domain → ``(lower, upper, levels, value)``.

    Maps the ``vary`` block kinds (range / list / distribution / subset /
    file_list / column) and the implicit bool sweep to display values.
    """
    if vary is None:
        return None, None, ([False, True] if ftype == "bool" else []), None
    kind = vary.get("kind")
    if kind == "range":
        return vary.get("min"), vary.get("max"), [], None
    if kind == "list":
        return None, None, list(vary.get("values") or []), None
    if kind == "distribution":
        low, high = vary.get("low"), vary.get("high")
        if low is None and high is None and vary.get("mean") is not None:
            return None, None, [], f"normal(mean={vary.get('mean')}, std={vary.get('std')})"
        return low, high, [], None
    if kind == "subset":
        return None, None, list(vary.get("options") or []), None
    if kind == "file_list":
        return None, None, list(vary.get("cas_hashes") or []), None
    if kind == "column":
        return None, None, [], "(values from run table)"
    return None, None, [], None


def _study_factor_variable(name: str, spec: Any, maps: DescriptorMaps) -> ApprovalVariable:
    """One parametric-study factor → a labelled :class:`ApprovalVariable`.

    Resolves the descriptor label + unit (never the raw input name) and
    the variation domain — a numeric range, an explicit level set, a
    categorical subset, or a held-constant value (``factor_spec`` §3).
    """
    label = maps.input_label(name)
    unit = maps.input_unit(name)
    if not isinstance(spec, dict):
        return ApprovalVariable(name=name, label=label, unit=unit, role="constant")
    ftype = spec.get("type")
    var_type: Literal["continuous", "integer", "categorical"] | None
    if ftype == "numeric":
        var_type = "continuous"
    elif ftype in ("select", "bool", "file"):
        var_type = "categorical"
    else:
        var_type = None
    if spec.get("role") != "vary":
        return ApprovalVariable(
            name=name,
            label=label,
            unit=unit,
            role="constant",
            var_type=var_type,
            value=spec.get("value"),
        )
    raw_vary = spec.get("vary")
    lower, upper, levels, value = _study_vary_domain(
        raw_vary if isinstance(raw_vary, dict) else None, ftype
    )
    return ApprovalVariable(
        name=name,
        label=label,
        unit=unit,
        role="vary",
        var_type=var_type,
        lower=lower,
        upper=upper,
        levels=levels,
        value=value,
    )


async def _study_approval_detail(db: AsyncSession, request: ApprovalRequest) -> ApprovalDetail:
    """Review panel for a study-level gate (design §7).

    The approver reviews the *plan* (pre) — mode, run cap, the
    generated/skipped counts, and the per-factor roles — and a
    run-outcome tally (post). The rich Results explorer is 2-PS-b; this
    panel carries the canonical-enough summary to decide release.
    """
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    study = await db.get(ParametricStudy, request.parametric_study_id)
    if study is None:
        raise NotFoundError("parametric study", request.parametric_study_id or 0)
    project = await db.get(Project, study.project_id)
    tool = (
        await db.execute(select(Tool).where(Tool.alias == request.tool_alias))
    ).scalar_one_or_none()
    requestor = await db.get(User, request.requested_by_user_id)
    tool_version = await db.get(ToolVersion, request.tool_version_id)
    maps = await descriptor_maps(db, request.tool_version_id)

    plan = ApprovalInputGroup(
        label="Sampling plan",
        rows=[
            ApprovalInputRow(name="mode", label="Sampling mode", value=study.mode),
            ApprovalInputRow(name="run_cap", label="Run cap", value=study.run_cap),
            ApprovalInputRow(name="generated", label="Jobs to run", value=study.generated_count),
            ApprovalInputRow(
                name="skipped",
                label="Points skipped (cross-field)",
                value=study.skipped_invalid_count,
            ),
        ],
    )
    # Factors resolved to labels + units + ranges/levels (not raw names).
    factors = [
        _study_factor_variable(str(fname), fspec, maps)
        for fname, fspec in (study.factor_spec or {}).items()
    ]

    result_groups: list[ApprovalResultGroup] = []
    run_matrix: ApprovalRunMatrix | None = None
    if request.kind == "post":
        children = list(
            (await db.execute(select(Job).where(Job.parametric_study_id == study.id))).scalars()
        )
        tally: dict[str, int] = {}
        for c in children:
            tally[c.state] = tally.get(c.state, 0) + 1
        result_groups = [
            ApprovalResultGroup(
                label="Run outcomes",
                rows=[
                    ApprovalResultRow(name=state, label=state, value=count)
                    for state, count in sorted(tally.items())
                ],
            )
        ]
        # Condensed one-row-per-run matrix (§9): varied factors + every
        # produced scalar output, labels + units, failures/outliers flagged.
        results = list(
            (
                await db.execute(
                    select(StudyResult).where(
                        StudyResult.source_kind == "parametric",
                        StudyResult.source_id == study.id,
                    )
                )
            ).scalars()
        )
        out_keys = sorted({k for r in results for k in (r.scalars or {})})
        run_matrix = await _build_run_matrix(
            db,
            study_id=study.id,
            source_kind="parametric",
            home_attr=Job.parametric_study_id,
            input_cols=[(v.name, v.label, v.unit) for v in factors if v.role == "vary"],
            output_cols=[(k, maps.output_label(k), maps.output_unit(k)) for k in out_keys],
        )

    return ApprovalDetail(
        request_id=request.id,
        kind=request.kind,  # type: ignore[arg-type]
        target_kind="study",
        status=request.status,
        job_id=None,
        study_id=study.id,
        job_name=study.name,
        tool_alias=request.tool_alias,
        tool_name=tool.name if tool is not None else request.tool_alias,
        tool_version=str(
            (tool_version.version if tool_version is not None else "")
            or (tool.version if tool is not None else "")
        ),
        project_name=project.name if project is not None else "",
        scenario_name="",
        requestor_name=_display_name(requestor),
        requested_at=request.requested_at,
        justification=request.justification or "",
        cpu_cores=0,
        ram_mb=0,
        gpu_memory_mb=0,
        licences=[],
        input_groups=[plan],
        result_groups=result_groups,
        factors=factors,
        run_matrix=run_matrix,
    )


def _tool_less_detail(
    request: ApprovalRequest,
    *,
    target_kind: str,
    target_name: str,
    requestor: User | None,
    input_groups: list[ApprovalInputGroup],
) -> ApprovalDetail:
    """Assemble the (tool-less) review panel shared by library + material gates."""
    return ApprovalDetail(
        request_id=request.id,
        kind=request.kind,  # type: ignore[arg-type]
        target_kind=target_kind,  # type: ignore[arg-type]
        status=request.status,
        job_id=None,
        study_id=None,
        job_name=target_name,
        tool_alias=None,
        tool_name=None,
        tool_version="",
        project_name="",
        scenario_name="",
        requestor_name=_display_name(requestor),
        requested_at=request.requested_at,
        justification=request.justification or "",
        cpu_cores=0,
        ram_mb=0,
        gpu_memory_mb=0,
        licences=[],
        input_groups=input_groups,
        result_groups=[],
    )


async def _library_approval_detail(db: AsyncSession, request: ApprovalRequest) -> ApprovalDetail:
    """Review panel for a global-library publish gate (ADR-0006 §5)."""
    version = await db.get(LibraryFileVersion, request.library_file_version_id)
    if version is None:
        raise NotFoundError("library file version", request.library_file_version_id or 0)
    lib_file = await db.get(LibraryFile, version.library_file_id)
    requestor = await db.get(User, request.requested_by_user_id)
    rows = [
        ApprovalInputRow(name="name", label="File", value=lib_file.name if lib_file else ""),
        ApprovalInputRow(name="scope", label="Target scope", value="global"),
        ApprovalInputRow(name="version", label="Version", value=version.ordinal),
        ApprovalInputRow(name="filename", label="Filename", value=version.original_filename),
        ApprovalInputRow(name="extension", label="Type", value=version.extension),
        ApprovalInputRow(name="size_bytes", label="Size (bytes)", value=version.size_bytes),
        ApprovalInputRow(name="provenance", label="Provenance", value=version.provenance),
        ApprovalInputRow(name="reference", label="Reference", value=version.reference),
    ]
    name = f"{lib_file.name} (v{version.ordinal})" if lib_file else "Library file"
    return _tool_less_detail(
        request,
        target_kind="library",
        target_name=name,
        requestor=requestor,
        input_groups=[ApprovalInputGroup(name="library_file", label="Library file", rows=rows)],
    )


async def _tool_vv_approval_detail(db: AsyncSession, request: ApprovalRequest) -> ApprovalDetail:
    """Review panel for a tool-V&V record promotion (ADR-0011 §3)."""
    record = await db.get(ToolVVRecord, request.tool_vv_record_id)
    if record is None:
        raise NotFoundError("tool V&V record", request.tool_vv_record_id or 0)
    tool_version = await db.get(ToolVersion, record.tool_version_id)
    tool = (
        (
            await db.execute(select(Tool).where(Tool.alias == tool_version.alias))
        ).scalar_one_or_none()
        if tool_version is not None
        else None
    )
    requestor = await db.get(User, request.requested_by_user_id)
    envelope = record.applicable_range_json or {}
    envelope_text = (
        ", ".join(f"{k}: {b.get('min', '?')} to {b.get('max', '?')}" for k, b in envelope.items())
        if envelope
        else "(none — unbounded)"
    )
    standards = ", ".join(record.standards_cited_json or []) or "(none cited)"
    rows = [
        ApprovalInputRow(
            name="tool",
            label="Tool",
            value=(
                f"{tool.name if tool else tool_version.alias} {tool_version.version}"
                if tool_version is not None
                else f"Tool version {record.tool_version_id}"
            ),
        ),
        ApprovalInputRow(name="kind", label="Kind", value=record.kind),
        ApprovalInputRow(name="envelope", label="Applicable range", value=envelope_text),
        ApprovalInputRow(
            name="confidence_band",
            label="Confidence band (±)",
            value=record.confidence_band,
        ),
        ApprovalInputRow(name="standards", label="Standards cited", value=standards),
        ApprovalInputRow(
            name="evidence",
            label="Evidence",
            value=(
                f"library_file_version={record.evidence_file_version_id}"
                if record.evidence_file_version_id
                else (record.evidence_url or "(none)")
            ),
        ),
        ApprovalInputRow(name="notes", label="Notes", value=record.notes or ""),
    ]
    name = (
        f"{record.kind.capitalize()} on "
        f"{tool_version.alias if tool_version else 'tool'} "
        f"{tool_version.version if tool_version else ''}"
    ).strip()
    return _tool_less_detail(
        request,
        target_kind="vv_tool",
        target_name=name,
        requestor=requestor,
        input_groups=[ApprovalInputGroup(name="vv_record", label="V&V record", rows=rows)],
    )


async def _job_vv_approval_detail(db: AsyncSession, request: ApprovalRequest) -> ApprovalDetail:
    """Review panel for a job-V&V record promotion (ADR-0011 §3)."""
    record = await db.get(JobVVRecord, request.job_vv_record_id)
    if record is None:
        raise NotFoundError("job V&V record", request.job_vv_record_id or 0)
    job = await db.get(Job, record.job_id)
    requestor = await db.get(User, request.requested_by_user_id)
    rows = [
        ApprovalInputRow(
            name="job",
            label="Job",
            value=job.name if job is not None else f"Job {record.job_id}",
        ),
        ApprovalInputRow(name="kind", label="Kind", value=record.kind),
        ApprovalInputRow(
            name="evidence",
            label="Evidence",
            value=(
                f"library_file_version={record.evidence_file_version_id}"
                if record.evidence_file_version_id
                else (record.evidence_url or "(none)")
            ),
        ),
        ApprovalInputRow(name="notes", label="Notes", value=record.notes or ""),
    ]
    name = (f"{record.kind.replace('_', ' ').capitalize()} on {job.name if job else 'job'}").strip()
    return _tool_less_detail(
        request,
        target_kind="vv_job",
        target_name=name,
        requestor=requestor,
        input_groups=[ApprovalInputGroup(name="vv_record", label="V&V record", rows=rows)],
    )


async def _material_approval_detail(db: AsyncSession, request: ApprovalRequest) -> ApprovalDetail:
    """Review panel for a material-change gate (ADR-0007 §5)."""
    version = await db.get(MaterialVersion, request.material_version_id)
    if version is None:
        raise NotFoundError("material version", request.material_version_id or 0)
    material = await db.get(Material, version.material_id)
    cls = await db.get(MaterialClass, material.class_id) if material is not None else None
    requestor = await db.get(User, request.requested_by_user_id)
    meta_rows = [
        ApprovalInputRow(name="name", label="Material", value=material.name if material else ""),
        ApprovalInputRow(name="class", label="Class", value=cls.name if cls else ""),
        ApprovalInputRow(name="version", label="Version", value=version.ordinal),
        ApprovalInputRow(name="provenance", label="Provenance", value=version.provenance),
        ApprovalInputRow(name="data_pedigree", label="Data pedigree", value=version.data_pedigree),
        ApprovalInputRow(name="reference", label="Reference", value=version.reference),
    ]
    props = (
        (
            await db.execute(
                select(MaterialProperty)
                .where(MaterialProperty.material_version_id == version.id)
                .order_by(MaterialProperty.prop_name)
            )
        )
        .scalars()
        .all()
    )
    prop_rows = [
        ApprovalInputRow(name=p.prop_name, label=p.prop_name, unit=p.unit, value=float(p.value))
        for p in props
    ]
    groups = [ApprovalInputGroup(name="material", label="Material", rows=meta_rows)]
    if prop_rows:
        groups.append(ApprovalInputGroup(name="properties", label="Properties", rows=prop_rows))
    name = f"{material.name} (v{version.ordinal})" if material else "Material"
    return _tool_less_detail(
        request,
        target_kind="material",
        target_name=name,
        requestor=requestor,
        input_groups=groups,
    )


def _resources(snapshot: dict[str, Any]) -> tuple[int, int, int, list[ApprovalLicence]]:
    """CPU / RAM / GPU + licences from a descriptor snapshot (shared with jobs)."""
    res = snapshot.get("resources") or {}
    cpu = int(res.get("cpu_cores", 1) or 1)
    ram = int(res.get("ram_mb", 512) or 512)
    gpu = int(res.get("gpu_memory_mb", 0) or 0)
    licences = [
        ApprovalLicence(name=str(lic.get("name", "")), count=int(lic.get("count", 1) or 1))
        for lic in (snapshot.get("licences_required") or [])
        if isinstance(lic, dict) and lic.get("name")
    ]
    return cpu, ram, gpu, licences


def _opt_variable(f: dict[str, Any], maps: DescriptorMaps) -> ApprovalVariable:
    """One varied factor → a labelled :class:`ApprovalVariable` (range / levels)."""
    name = str(f["name"])
    ftype = f.get("type")
    if ftype == "categorical":
        return ApprovalVariable(
            name=name,
            label=maps.input_label(name),
            unit=maps.input_unit(name),
            role="vary",
            var_type="categorical",
            levels=list(f.get("choices") or []),
        )
    return ApprovalVariable(
        name=name,
        label=maps.input_label(name),
        unit=maps.input_unit(name),
        role="vary",
        var_type="integer" if ftype == "int" else "continuous",
        lower=f.get("min"),
        upper=f.get("max"),
    )


def _opt_objectives(
    config: dict[str, Any], maps: DescriptorMaps
) -> tuple[list[ApprovalObjective], dict[str, str]]:
    """Responses → objectives + a response-name→output-name map for constraints."""
    resp_output: dict[str, str] = {}
    objectives: list[ApprovalObjective] = []
    for r in config.get("responses") or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        name = str(r["name"])
        spec = r.get("spec")
        if not isinstance(spec, dict):
            spec = {}
        out_name = str(spec.get("name", name)) if spec.get("kind") == "scalar" else name
        resp_output[name] = out_name
        if r.get("goal") in ("minimise", "maximise", "track"):
            objectives.append(
                ApprovalObjective(
                    name=name,
                    label=maps.output_label(out_name),
                    unit=maps.output_unit(out_name),
                    sense=r["goal"],
                )
            )
    return objectives, resp_output


def _opt_constraint(
    c: dict[str, Any], maps: DescriptorMaps, resp_output: dict[str, str]
) -> ApprovalConstraint | None:
    """One constraint → a human-rendered :class:`ApprovalConstraint`."""
    kind = c.get("kind")
    if kind == "output":
        out_name = resp_output.get(str(c.get("response", "")), str(c.get("response", "")))
        return ApprovalConstraint(
            label=maps.output_label(out_name),
            unit=maps.output_unit(out_name),
            kind="output",
            op=c.get("op"),
            value=c.get("value"),
        )
    if kind == "cross_field":
        return ApprovalConstraint(
            label="Cross-field constraint", kind="cross_field", expression=c.get("expr")
        )
    if kind == "box":
        return ApprovalConstraint(
            label="Box constraint",
            kind="box",
            expression=", ".join(f"{k}={v}" for k, v in c.items() if k != "kind"),
        )
    return None


def _opt_strategy(config: dict[str, Any]) -> ApprovalStrategy:
    """The strategy block, in human terms."""
    code = config.get("strategy")
    mop = config.get("mop") if isinstance(config.get("mop"), dict) else {}
    return ApprovalStrategy(
        algorithm=_STRATEGY_LABELS.get(str(code), str(code or "—")),
        acquisition=(
            _ACQUISITION_LABELS.get(str(config.get("acquisition", "balanced")))
            if code == "bo"
            else None
        ),
        batch_size=config.get("batch_size"),
        run_mode=_RUN_MODE_LABELS.get(str(config.get("run_mode"))),
        variable_reduction="MOP/CoP variable reduction" if mop and mop.get("enabled") else None,
    )


def _opt_stopping(config: dict[str, Any]) -> tuple[ApprovalStopping, bool]:
    """The budget + seeding in plain words; also whether convergence can stop early."""
    criteria: list[str] = []
    has_convergence = False
    for crit in config.get("stopping_criteria") or []:
        if not isinstance(crit, dict):
            continue
        if crit.get("kind") == "max_runs":
            criteria.append(f"Max {crit.get('value')} evaluations")
        elif crit.get("kind") == "convergence":
            has_convergence = True
            metric = _CONVERGENCE_LABELS.get(str(crit.get("metric")), str(crit.get("metric")))
            criteria.append(f"Converge on {metric}")
    seed = config.get("seed") if isinstance(config.get("seed"), dict) else {}
    seeding: str | None = None
    if seed and seed.get("kind") == "cohort":
        seeding = "Seed from a job cohort"
    elif seed and seed.get("kind"):
        seeding = f"{seed.get('n')} {str(seed.get('kind')).upper()} seed points"
    return ApprovalStopping(criteria=criteria, seeding=seeding), has_convergence


def _build_optimisation_review(
    config: dict[str, Any],
    maps: DescriptorMaps,
    snapshot: dict[str, Any],
    *,
    is_workflow: bool,
) -> ApprovalOptimisationReview:
    """Structured optimisation review block from the frozen ``config_json``.

    Factors → varied :class:`ApprovalVariable`s with ranges/levels;
    responses → objectives (label + sense); output constraints → human
    "label op value unit" lines; strategy / stopping in plain words; and
    a best-effort compute footprint (§5). Labels resolve through the
    pinned tool version's descriptor maps — never the internal name.
    """
    variables = [
        _opt_variable(f, maps)
        for f in (config.get("factors") or [])
        if isinstance(f, dict) and f.get("name")
    ]
    objectives, resp_output = _opt_objectives(config, maps)
    constraints = [
        con
        for c in (config.get("constraints") or [])
        if isinstance(c, dict)
        for con in (_opt_constraint(c, maps, resp_output),)
        if con is not None
    ]
    stopping, has_convergence = _opt_stopping(config)
    footprint = _optimisation_footprint(
        config, snapshot, has_convergence=has_convergence, is_workflow=is_workflow
    )
    return ApprovalOptimisationReview(
        variables=variables,
        objectives=objectives,
        constraints=constraints,
        strategy=_opt_strategy(config),
        stopping=stopping,
        footprint=footprint,
    )


def _optimisation_footprint(
    config: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    has_convergence: bool,
    is_workflow: bool,
) -> ApprovalFootprint:
    """Best-effort compute blast-radius (§5).

    ``max_runs`` is mandatory, so the upper bound is firm; a convergence
    criterion can stop earlier, hence the min/max range. Core-hours is left
    ``None`` (the descriptor declares no walltime — never present a guess as
    exact). A workflow target is marked approximate (each eval is a chain).
    """
    max_runs = 0
    for crit in config.get("stopping_criteria") or []:
        if isinstance(crit, dict) and crit.get("kind") == "max_runs":
            max_runs = int(crit.get("value") or 0)
            break
    seed = config.get("seed") if isinstance(config.get("seed"), dict) else {}
    seed_runs = int(seed.get("n") or 0) if seed and seed.get("kind") != "cohort" else 0
    batch = int(config.get("batch_size", 1) or 1)
    est_max = max_runs + seed_runs
    est_min = min(seed_runs + batch, est_max) if has_convergence else est_max

    cpu, ram, gpu, licences = _resources(snapshot)
    note: str | None = None
    approximate = False
    if is_workflow:
        approximate = True
        note = (
            "Workflow target — each evaluation runs the whole tool chain; "
            "per-run figures are the chain's peak."
        )
    return ApprovalFootprint(
        est_runs_min=est_min,
        est_runs_max=est_max,
        cpu_cores_per_run=cpu,
        ram_mb_per_run=ram,
        gpu_memory_mb_per_run=gpu or None,
        est_core_hours=None,
        licences=licences,
        approximate=approximate,
        note=note,
    )


async def _optimisation_approval_detail(
    db: AsyncSession, request: ApprovalRequest
) -> ApprovalDetail:
    """Review panel for an optimisation start / results gate (ADR-0009 §14.1)."""
    study = await db.get(OptimisationStudy, request.optimisation_study_id)
    if study is None:
        raise NotFoundError("optimisation study", request.optimisation_study_id or 0)
    project = await db.get(Project, study.project_id)
    tool = (
        (await db.execute(select(Tool).where(Tool.alias == study.tool_alias))).scalar_one_or_none()
        if study.tool_alias
        else None
    )
    requestor = await db.get(User, request.requested_by_user_id)
    tool_version = (
        await db.get(ToolVersion, study.tool_version_id)
        if study.tool_version_id is not None
        else None
    )
    snapshot = (
        dict(tool_version.descriptor_snapshot)
        if tool_version is not None and tool_version.descriptor_snapshot
        else {}
    )
    maps = await descriptor_maps(db, study.tool_version_id)
    config = dict(study.config_json or {})
    review = _build_optimisation_review(
        config, maps, snapshot, is_workflow=study.target_kind == "workflow"
    )
    fp = review.footprint
    # Results-release (post) gate: condensed evaluations matrix — varied
    # factors + objective values per run, labels + units (§9).
    run_matrix: ApprovalRunMatrix | None = None
    if request.kind == "post":
        run_matrix = await _build_run_matrix(
            db,
            study_id=study.id,
            source_kind="optimisation",
            home_attr=Job.optimisation_study_id,
            input_cols=[
                (str(f["name"]), maps.input_label(str(f["name"])), maps.input_unit(str(f["name"])))
                for f in (config.get("factors") or [])
                if isinstance(f, dict) and f.get("name")
            ],
            output_cols=[(o.name, o.label, o.unit) for o in review.objectives],
        )
    return ApprovalDetail(
        request_id=request.id,
        kind=request.kind,  # type: ignore[arg-type]
        target_kind="optimisation",
        status=request.status,
        job_id=None,
        study_id=None,
        optimisation_study_id=study.id,
        job_name=study.name or f"Optimisation study {study.id}",
        tool_alias=study.tool_alias,
        tool_name=tool.name if tool is not None else study.tool_alias,
        tool_version=str(
            (tool_version.version if tool_version is not None else "")
            or (tool.version if tool is not None else "")
        ),
        project_name=project.name if project is not None else "",
        scenario_name="",
        requestor_name=_display_name(requestor),
        requested_at=request.requested_at,
        justification=request.justification or "",
        cpu_cores=fp.cpu_cores_per_run or 0 if fp is not None else 0,
        ram_mb=fp.ram_mb_per_run or 0 if fp is not None else 0,
        gpu_memory_mb=fp.gpu_memory_mb_per_run or 0 if fp is not None else 0,
        licences=fp.licences if fp is not None else [],
        input_groups=[],
        result_groups=[],
        optimisation=review,
        run_matrix=run_matrix,
    )


async def _surrogate_approval_detail(db: AsyncSession, request: ApprovalRequest) -> ApprovalDetail:
    """Review panel for a surrogate-model publish gate (ADR-0008 §6.2, D5)."""
    version = await db.get(SurrogateModelVersion, request.surrogate_model_version_id)
    if version is None:
        raise NotFoundError("surrogate model version", request.surrogate_model_version_id or 0)
    model = await db.get(SurrogateModel, version.surrogate_model_id)
    tool = (
        (await db.execute(select(Tool).where(Tool.alias == model.tool_alias))).scalar_one_or_none()
        if model is not None and model.tool_alias
        else None
    )
    requestor = await db.get(User, request.requested_by_user_id)
    # Resolve labels/units from the descriptor pinned at fit time.
    fit_versions = version.tool_version_ids_at_fit or []
    maps = await descriptor_maps(db, fit_versions[0] if fit_versions else None)

    predicts = [
        maps.output_label(str(r.get("name")))
        for r in (version.responses or [])
        if isinstance(r, dict) and r.get("name")
    ]
    domain = [
        ApprovalVariable(
            name=str(f.get("name")),
            label=maps.input_label(str(f.get("name"))),
            unit=f.get("unit") or maps.input_unit(str(f.get("name"))),
            role="vary",
            lower=f.get("min"),
            upper=f.get("max"),
        )
        for f in (version.factors or [])
        if isinstance(f, dict) and f.get("name")
    ]
    metrics = _surrogate_metrics(version, maps)
    training_source = await _surrogate_training_source(db, version)
    review = ApprovalSurrogateReview(
        model_kind=_SURROGATE_METHOD_LABELS.get(
            str(version.surrogate_method or "gp"), str(version.surrogate_method or "gp")
        ),
        predicts=predicts,
        training_samples=version.n_training_points,
        training_source=training_source,
        domain=domain,
        metrics=metrics,
    )
    name = (
        f"{model.name} (v{version.ordinal})"
        if model is not None
        else f"Surrogate model version {version.id}"
    )
    return ApprovalDetail(
        request_id=request.id,
        kind=request.kind,  # type: ignore[arg-type]
        target_kind="surrogate_model",
        status=request.status,
        job_id=None,
        study_id=None,
        job_name=name,
        tool_alias=model.tool_alias if model is not None else None,
        tool_name=tool.name if tool is not None else (model.tool_alias if model else None),
        tool_version="",
        project_name="",
        scenario_name="",
        requestor_name=_display_name(requestor),
        requested_at=request.requested_at,
        justification=request.justification or "",
        cpu_cores=0,
        ram_mb=0,
        gpu_memory_mb=0,
        licences=[],
        input_groups=[],
        result_groups=[],
        surrogate=review,
    )


def _surrogate_metrics(
    version: SurrogateModelVersion, maps: DescriptorMaps
) -> list[ApprovalSurrogateMetric]:
    """Per-response validation metrics from the version's ``quality`` dict.

    Scalar fits carry cross-validated Q² + RMSE per response; field fits
    carry a reconstruction RMSE + retained-mode energy. Degrade silently
    when a metric is missing rather than fabricate one.
    """
    quality = version.quality if isinstance(version.quality, dict) else {}
    out: list[ApprovalSurrogateMetric] = []
    for resp_name, q in quality.items():
        if not isinstance(q, dict):
            continue
        label = maps.output_label(str(resp_name))
        unit = maps.output_unit(str(resp_name))
        if q.get("q2") is not None:
            out.append(
                ApprovalSurrogateMetric(
                    response_label=label, metric="Q² (cross-validated)", value=float(q["q2"])
                )
            )
        if q.get("rmse_cv") is not None:
            out.append(
                ApprovalSurrogateMetric(
                    response_label=label, unit=unit, metric="RMSE (CV)", value=float(q["rmse_cv"])
                )
            )
        if q.get("recon_rmse") is not None:
            out.append(
                ApprovalSurrogateMetric(
                    response_label=label,
                    unit=unit,
                    metric="Reconstruction RMSE",
                    value=float(q["recon_rmse"]),
                )
            )
        if q.get("mode_energy") is not None:
            out.append(
                ApprovalSurrogateMetric(
                    response_label=label,
                    metric="Retained mode energy",
                    value=float(q["mode_energy"]),
                )
            )
    return out


async def _surrogate_training_source(
    db: AsyncSession, version: SurrogateModelVersion
) -> str | None:
    """Human name of the study / cohort the fit was trained on (best-effort)."""
    kind = version.source_kind
    sid = version.source_id
    if kind == "cohort":
        from matofx.studies.cohort.orm import JobCohort  # noqa: PLC0415

        cohort = await db.get(JobCohort, sid)
        return f"Job cohort “{cohort.name}”" if cohort is not None else f"Job cohort #{sid}"
    if kind == "optimisation":
        opt = await db.get(OptimisationStudy, sid)
        return (
            f"Optimisation study “{opt.name}”" if opt is not None else f"Optimisation study #{sid}"
        )
    if kind in ("study", "parametric"):
        ps = await db.get(ParametricStudy, sid)
        return f"Parametric study “{ps.name}”" if ps is not None else f"Parametric study #{sid}"
    return f"{str(kind).replace('_', ' ').capitalize()} #{sid}" if kind else None


def _build_review(
    snapshot: dict[str, Any], inputs: dict[str, Any]
) -> tuple[int, int, int, list[ApprovalLicence], list[ApprovalInputGroup]]:
    """Build the grouped review payload from a snapshot + submitted values.

    Read defensively as a plain mapping (a snapshot may be a minimal /
    legacy descriptor): missing resources fall back to the descriptor
    defaults, an undeclared input still shows under "Other", and a
    declared input the user left blank shows its default.
    """
    resources = snapshot.get("resources") or {}
    cpu = int(resources.get("cpu_cores", 1) or 1)
    ram = int(resources.get("ram_mb", 512) or 512)
    gpu = int(resources.get("gpu_memory_mb", 0) or 0)

    licences = [
        ApprovalLicence(
            name=str(lic.get("name", "")),
            count=int(lic.get("count", 1) or 1),
        )
        for lic in (snapshot.get("licences_required") or [])
        if isinstance(lic, dict) and lic.get("name")
    ]

    fields = [f for f in (snapshot.get("inputs") or []) if isinstance(f, dict)]
    group_defs = [g for g in (snapshot.get("input_groups") or []) if isinstance(g, dict)]
    group_labels: dict[str, str] = {
        str(g.get("name")): str(g.get("label") or g.get("name"))
        for g in group_defs
        if g.get("name")
    }

    declared_names = {str(f.get("name")) for f in fields if f.get("name")}
    buckets: dict[str | None, list[ApprovalInputRow]] = {}
    order: list[str | None] = []

    def _row(field: dict[str, Any]) -> ApprovalInputRow:
        name = str(field.get("name"))
        default = field.get("default")
        value = inputs.get(name, default)
        # "changed" only means something when the field actually
        # declares a default to deviate from. Tools that declare no
        # defaults (default=None) would otherwise flag *every* row,
        # turning the approver's attention marker into pure noise.
        has_default = default is not None
        return ApprovalInputRow(
            name=name,
            label=str(field.get("label") or name),
            unit=field.get("unit"),
            value=value,
            default=default,
            changed_from_default=has_default and value != default,
        )

    for field in fields:
        if not field.get("name"):
            continue
        grp = field.get("group")
        key = str(grp) if grp else None
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(_row(field))

    # Submitted values with no matching declared input (e.g. the
    # descriptor changed after submit) — surface them so the approver
    # sees exactly what was sent, never a hidden value.
    extras = [
        ApprovalInputRow(name=k, label=k, value=v)
        for k, v in inputs.items()
        if k not in declared_names
    ]
    if extras:
        if None not in buckets:
            buckets[None] = []
            order.append(None)
        buckets[None].extend(extras)

    groups: list[ApprovalInputGroup] = []
    for key in order:
        label = "Other" if key is None else group_labels.get(key, key)
        groups.append(ApprovalInputGroup(name=key, label=label, rows=buckets[key]))
    return cpu, ram, gpu, licences, groups


def _build_results(
    snapshot: dict[str, Any], outputs_doc: dict[str, Any]
) -> list[ApprovalResultGroup]:
    """Grouped produced-results payload — the ``post`` reviewer's view.

    Mirrors :func:`_build_review`: declared outputs are laid out in
    their descriptor groups (a declared output the run didn't emit
    still shows, blank); a produced value with no matching declared
    output surfaces under "Other" so the approver never reviews a
    release with a hidden result. The produced document is
    authoritative for the actual value/filename; the descriptor
    supplies the human label/unit/grouping.
    """
    produced = {
        str(o.get("name")): o
        for o in (outputs_doc.get("outputs") or [])
        if isinstance(o, dict) and o.get("name")
    }
    fields = [f for f in (snapshot.get("outputs") or []) if isinstance(f, dict)]
    group_defs = [g for g in (snapshot.get("output_groups") or []) if isinstance(g, dict)]
    group_labels: dict[str, str] = {
        str(g.get("name")): str(g.get("label") or g.get("name"))
        for g in group_defs
        if g.get("name")
    }

    declared_names = {str(f.get("name")) for f in fields if f.get("name")}
    buckets: dict[str | None, list[ApprovalResultRow]] = {}
    order: list[str | None] = []

    def _row(name: str, field: dict[str, Any]) -> ApprovalResultRow:
        out = produced.get(name) or {}
        return ApprovalResultRow(
            name=name,
            label=str(field.get("label") or out.get("label") or name),
            unit=field.get("unit") or out.get("unit"),
            value=out.get("value"),
            filename=out.get("filename") or field.get("filename"),
        )

    for field in fields:
        name = field.get("name")
        if not name:
            continue
        grp = field.get("group")
        key = str(grp) if grp else None
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(_row(str(name), field))

    extras = [
        ApprovalResultRow(
            name=str(o.get("name")),
            label=str(o.get("label") or o.get("name")),
            unit=o.get("unit"),
            value=o.get("value"),
            filename=o.get("filename"),
        )
        for nm, o in produced.items()
        if nm not in declared_names
    ]
    if extras:
        if None not in buckets:
            buckets[None] = []
            order.append(None)
        buckets[None].extend(extras)

    result_groups: list[ApprovalResultGroup] = []
    for key in order:
        label = "Other" if key is None else group_labels.get(key, key)
        result_groups.append(ApprovalResultGroup(name=key, label=label, rows=buckets[key]))
    return result_groups


# --- condensed multi-run review (§9) ----------------------------------------

_Col = tuple[str, str, str | None]  # (key, label, unit)
_OUTLIER_SIGMA = 3.0


def _numeric(value: Any) -> float | None:
    """A finite float, or ``None`` — booleans and non-numbers are not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if _is_finite(f) else None


def _is_finite(f: float) -> bool:
    return f == f and f not in (float("inf"), float("-inf"))  # noqa: PLR0124


def _outlier_bounds(values: list[float]) -> tuple[float, float] | None:
    """Mean +/- 3-sigma band, or ``None`` when there's too little spread to judge."""
    if len(values) < 3:  # noqa: PLR2004 — need a few points before "outlier" means anything
        return None
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    if sd == 0:
        return None
    return mean - _OUTLIER_SIGMA * sd, mean + _OUTLIER_SIGMA * sd


async def _build_run_matrix(
    db: AsyncSession,
    *,
    study_id: int,
    source_kind: str,
    home_attr: Any,
    input_cols: list[_Col],
    output_cols: list[_Col],
) -> ApprovalRunMatrix:
    """Condensed one-row-per-run review for a study / optimisation post gate.

    ``home_attr`` is the ``Job`` column homing children to the parent
    (``Job.parametric_study_id`` / ``Job.optimisation_study_id``).
    Per-run output values come from the cheap :class:`StudyResult` store
    (no per-job output-document reads). Failed runs and per-output
    outliers (mean +/- 3 sigma) are flagged so the approver's eye lands on them.
    """
    jobs = list((await db.execute(select(Job).where(home_attr == study_id))).scalars())
    jobs.sort(key=lambda j: j.id)
    results = list(
        (
            await db.execute(
                select(StudyResult).where(
                    StudyResult.source_kind == source_kind,
                    StudyResult.source_id == study_id,
                )
            )
        ).scalars()
    )
    res_by_job = {r.job_id: r for r in results}

    def _scalars(job_id: int) -> dict[str, Any]:
        r = res_by_job.get(job_id)
        return (r.scalars if r is not None else {}) or {}

    # Outlier band per numeric output column, over completed runs.
    bounds: dict[str, tuple[float, float] | None] = {}
    for key, _, _ in output_cols:
        vals = [
            v
            for j in jobs
            if j.state == "complete"
            for v in (_numeric(_scalars(j.id).get(key)),)
            if v is not None
        ]
        bounds[key] = _outlier_bounds(vals)

    rows: list[ApprovalRunRow] = []
    failed = 0
    for j in jobs:
        scal = _scalars(j.id)
        inp = j.inputs or {}
        cells: dict[str, Any] = {k: inp.get(k) for k, _, _ in input_cols}
        is_outlier = False
        for key, _, _ in output_cols:
            cells[key] = scal.get(key)
            band = bounds.get(key)
            num = _numeric(scal.get(key))
            if band is not None and num is not None and not (band[0] <= num <= band[1]):
                is_outlier = True
        is_failed = j.state == "failed"
        failed += int(is_failed)
        rows.append(
            ApprovalRunRow(
                job_id=j.id,
                label=j.name or f"Job {j.id}",
                state=j.state,
                failed=is_failed,
                outlier=is_outlier and not is_failed,
                cells=cells,
            )
        )

    aggregates = [
        ApprovalRunAggregate(
            label=label,
            unit=unit,
            minimum=min(vals) if vals else None,
            maximum=max(vals) if vals else None,
            mean=round(statistics.fmean(vals), 6) if vals else None,
        )
        for key, label, unit in output_cols
        for vals in (
            [
                v
                for j in jobs
                if j.state == "complete"
                for v in (_numeric(_scalars(j.id).get(key)),)
                if v is not None
            ],
        )
    ]
    columns = [
        ApprovalRunColumn(key=k, label=lbl, unit=u, kind="input") for k, lbl, u in input_cols
    ] + [ApprovalRunColumn(key=k, label=lbl, unit=u, kind="output") for k, lbl, u in output_cols]
    return ApprovalRunMatrix(
        columns=columns,
        rows=rows,
        aggregates=aggregates,
        total=len(jobs),
        failed=failed,
        hidden=0,
    )


@router.post("/approvals/decisions", response_model=ApprovalDecisionsResponse)
async def submit_decisions(  # noqa: PLR0912, PLR0915 — a flat dispatch: one branch per gate target.
    body: ApprovalDecisionsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    library_service: Annotated[LibraryService, Depends(get_library_service)],
    materials_service: Annotated[MaterialsService, Depends(get_materials_service)],
    vv_service: Annotated[VVService, Depends(get_vv_service)],
    model_service: Annotated[ModelService, Depends(get_model_service)],
    optimisation_service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> ApprovalDecisionsResponse:
    """Apply a batch of approve/deny decisions, one savepoint each.

    Each decision is independent: an already-decided / not-permitted /
    missing-deny-reason item fails in isolation (rolled back to its
    savepoint) and is reported per-item; the rest of the batch still
    commits. The router routes by target — a study gate to
    :class:`StudyService`, a library gate to :class:`LibraryService`, a
    material gate to :class:`MaterialsService`, else a job gate to
    :class:`JobService` — and each applies the same
    ``ApprovalService.decide`` + three-layer self-approval defence plus
    its own publish/queue/freeze side-effects. Decided **job / study**
    gates feed one consolidated requestor email; tool-less (library /
    material) gates don't (no job-centric notice).
    """
    is_admin = await _is_admin(db, current_user)
    results: list[ApprovalDecisionResult] = []
    # (id, name, tool_alias, requestor_id, kind, decision, reason) — job/study only.
    decided: list[tuple[int, str, str, int, str, str, str | None]] = []
    # Shared decide kwargs minus ``decider_is_approver`` — that fact is
    # gate-kind-specific: the tool-approver flag for job / study gates,
    # the curate capability for library / material gates.
    common = {
        "decider_user_id": current_user.id,
        "decider_username": current_user.username or "",
        "decider_is_admin": is_admin,
    }
    is_tool_approver = bool(current_user.is_approver)
    is_lib_curator = await user_has_permission(db, current_user.id, Permission.LIBRARY_CURATE)
    is_mat_curator = await user_has_permission(db, current_user.id, Permission.MATERIALS_CURATE)
    is_vv_curator = await user_has_permission(db, current_user.id, Permission.VV_CURATE)
    is_models_curator = await user_has_permission(db, current_user.id, Permission.MODELS_CURATE)

    for item in body.decisions:
        try:
            notify_entry: tuple[int, str, str, int, str, str, str | None] | None = None
            async with db.begin_nested():
                probe = await approval_service.get(db, request_id=item.request_id)
                if probe.parametric_study_id is not None:
                    request, study = await study_service.decide_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_tool_approver,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, study.state
                    notify_entry = _notice(study.id, study.name, request, item.decision)
                elif probe.library_file_version_id is not None:
                    request, lib_version = await library_service.decide_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_lib_curator,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, lib_version.publish_state
                elif probe.material_version_id is not None:
                    request, mat_version = await materials_service.decide_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_mat_curator,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, mat_version.publish_state
                elif probe.tool_vv_record_id is not None:
                    request, tool_vv = await vv_service.decide_tool_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_vv_curator,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, tool_vv.state
                elif probe.job_vv_record_id is not None:
                    request, job_vv = await vv_service.decide_job_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_vv_curator,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, job_vv.state
                elif probe.surrogate_model_version_id is not None:
                    request, model_version = await model_service.decide_check(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_curator=is_models_curator,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, model_version.publish_state
                elif probe.optimisation_study_id is not None:
                    request, opt_study = await optimisation_service.decide_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_tool_approver,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = None, opt_study.state
                    notify_entry = _notice(opt_study.id, opt_study.name, request, item.decision)
                else:
                    request, job = await job_service.decide_approval(
                        db,
                        request_id=item.request_id,
                        decision=item.decision,
                        reason=item.reason,
                        decider_is_approver=is_tool_approver,
                        **common,  # type: ignore[arg-type]
                    )
                    result_job_id, result_state = job.id, job.state
                    notify_entry = _notice(job.id, job.name, request, item.decision)
            results.append(
                ApprovalDecisionResult(
                    request_id=item.request_id,
                    ok=True,
                    status=request.status,
                    job_id=result_job_id,
                    job_state=result_state,
                )
            )
            if notify_entry is not None:
                decided.append(notify_entry)
        except (
            ApprovalError,
            HierarchyError,
            StudyGenerationError,
            StudyStateError,
            LibraryError,
            MaterialsError,
            VVError,
            ModelError,
            OptimisationError,
        ) as exc:
            results.append(
                ApprovalDecisionResult(
                    request_id=item.request_id,
                    ok=False,
                    error=str(exc),
                )
            )

    # One consolidated email per requestor for the whole batch.
    # Fire-and-forget: a relay hiccup must not fail the decision that
    # already committed.
    if decided:
        try:
            aliases = {d[2] for d in decided}
            tool_names = {
                t.alias: t.name
                for t in (await db.execute(select(Tool).where(Tool.alias.in_(aliases)))).scalars()
            }
            notices = [
                ApprovalDecisionNotice(
                    job_id=jid,
                    job_name=jname,
                    tool_name=tool_names.get(alias, alias),
                    requestor_user_id=rid,
                    kind=kind,
                    decision=decision,
                    reason=reason,
                )
                for (jid, jname, alias, rid, kind, decision, reason) in decided
            ]
            await notifier.notify_approval_decisions(db, notices=notices)
        except Exception:
            _log.exception("approval decision notification failed; swallowed")

    return ApprovalDecisionsResponse(results=results)
