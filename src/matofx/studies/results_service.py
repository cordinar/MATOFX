"""Read + write services for the ``parametric_study_result`` store (2-PS-b0).

Two sides of the b0 results foundation:

- :func:`record_study_result` is the **runner's write hook** — called
  from ``Runner._post_complete`` BEFORE ``clean_directory`` runs, and
  also from the failure path so cancelled / failed children appear in
  the matrix with a null response (design §10.0 / D3 — partial-data
  banner, never silently dropped). No-op for non-study children
  (the early return makes the caller side trivial). Idempotent: a
  second call for the same ``job_id`` is a no-op (the UNIQUE on
  ``parametric_study_result.job_id`` would otherwise raise, but the
  runner can re-enter the finalisation path under race / retry).

- :class:`ResultsService` is the **read-side** for the API and UI
  (2-PS-b0 Results tab; 2-PS-b/c plots + sensitivity + surrogate are
  pure read-side over the same store). Gated by project VIEW —
  delegates the ACL check to :class:`StudyService.get`.

The store itself + its migration is b0-2 (``ParametricStudyResult``
ORM + ``0041_parametric_study_result.py``). ADR-0001 2026-05-19
revision / ADR-0003 / ``designs/parametric-studies.md`` §10.0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import AuditWriter
from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import Job
from matofx.studies.analysis import compute_sensitivity, varied_columns
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.studies.service import StudyService
from matofx.tools.exec.contracts import OutputsDocument
from matofx.tools.orm import Tool


def _is_number(value: Any) -> bool:
    """``True`` for a real numeric value — int / float but not bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sensitivity_weights_from_children(
    children: list[StudyChildResult],
) -> dict[str, float]:
    """Per-factor ``|SRC|`` weights from one study's children, or ``{}``.

    Self-contained over :class:`StudyChildResult`: builds the varied
    numeric-factor columns, then walks the study's numeric scalar
    responses (deterministic order) and returns the standardised
    regression coefficient magnitudes from the **first** response whose
    fit is sufficient and yields any coefficient. ``{}`` when no factor
    varies, there is no usable scalar response, or every fit is
    under-sampled — the caller falls back to uniform weighting.
    """
    if not children:
        return {}
    factor_names = sorted({k for c in children for k, v in c.inputs.items() if _is_number(v)})
    columns: dict[str, list[float | None]] = {
        n: [float(c.inputs[n]) if _is_number(c.inputs.get(n)) else None for c in children]
        for n in factor_names
    }
    varied = varied_columns(columns)
    if not varied:
        return {}
    columns = {n: columns[n] for n in varied}

    response_names = sorted({k for c in children for k, v in c.scalars.items() if _is_number(v)})
    for resp in response_names:
        values = [
            float(c.scalars[resp]) if _is_number(c.scalars.get(resp)) else None for c in children
        ]
        result = compute_sensitivity(resp, varied, columns, values)
        if not result.sufficient:
            continue
        weights = {f.factor: abs(f.std_coef) for f in result.factors if f.std_coef is not None}
        if weights:
            return weights
    return {}


@dataclass(frozen=True, slots=True)
class StudyChildResult:
    """One row of the study results matrix."""

    job_id: int
    state: str
    inputs: dict[str, Any]
    """The child's varied input values (from ``Job.inputs``)."""
    scalars: dict[str, Any]
    """``{output_name: scalar_value}`` — numeric + string outputs.
    Empty for failed / cancelled children (D3 null-with-banner)."""
    retained_files: list[dict[str, Any]]
    """File outputs that survived cleanup, with ``kind`` mirroring
    the descriptor's ``OutputField.file_type`` (``csv_plot`` files
    are the response-from-column + curve-overlay candidates)."""
    validity: str = "valid"
    """ADR-0017 output-validity verdict: ``valid`` | ``invalid``. ``valid``
    for children with no result row yet (still running / pre-store)."""
    validity_reasons: list[dict[str, Any]] = field(default_factory=list)
    """Why the row is invalid: ``[{output, code, detail}, ...]``; empty when
    valid. Drives the results-matrix health badge."""


@dataclass(frozen=True, slots=True)
class StudyResultsView:
    """The whole results matrix for one study.

    Carries the pinned ``tool_alias`` / ``tool_version_id`` so the API
    can resolve the descriptor for response-source metadata without a
    second ACL round-trip (the matrix call already enforced VIEW).
    """

    study_id: int
    tool_alias: str
    tool_version_id: int
    children: list[StudyChildResult]


# ---------------------------------------------------------------------------
# Write side — runner hook (b0-3)
# ---------------------------------------------------------------------------


async def record_study_result(
    audit: AuditWriter,
    session: AsyncSession,
    *,
    job: Job,
    tool: Tool,
    outputs_doc: OutputsDocument | None,
    succeeded: bool,
) -> None:
    """Persist one ``StudyResult`` row for a study child.

    No-op when ``job`` is not a study child (neither
    ``parametric_study_id`` nor ``optimisation_study_id`` set) or a row
    already exists for this ``(source, job)`` (idempotent under runner
    retries). Failed / cancelled children get an empty ``scalars`` dict
    and empty ``retained_files`` — they appear in the matrix with a null
    response (D3 / partial-data banner).

    The row is tagged with the matching discriminator (studies-engine
    §6): ``source_kind='parametric'`` for an ADR-0001 child,
    ``'optimisation'`` for an ADR-0009 child. The cohort sibling
    populates the same store via its own lazy path. A job is homed under
    exactly one study (the ``ck_job_exactly_one_home`` CHECK), so at most
    one discriminator applies.

    Called by :meth:`Runner._post_complete` on success (with the
    parsed ``OutputsDocument``) and by the failure-path branch (with
    ``outputs_doc=None``, ``succeeded=False``).
    """
    if job.parametric_study_id is not None:
        source_kind, source_id, resource_type = (
            "parametric",
            job.parametric_study_id,
            "parametric_study",
        )
    elif job.optimisation_study_id is not None:
        source_kind, source_id, resource_type = (
            "optimisation",
            job.optimisation_study_id,
            "optimisation_study",
        )
    else:
        return
    existing = (
        await session.execute(
            select(StudyResult.id).where(
                StudyResult.source_kind == source_kind,
                StudyResult.source_id == source_id,
                StudyResult.job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return  # idempotent — already recorded
    scalars = _extract_scalars(outputs_doc) if (succeeded and outputs_doc is not None) else {}
    retained_files = (
        _extract_retained_files(outputs_doc, tool)
        if (succeeded and outputs_doc is not None)
        else []
    )
    validity, validity_reasons = _evaluate_validity(scalars, tool)
    session.add(
        StudyResult(
            source_kind=source_kind,
            source_id=source_id,
            job_id=job.id,
            scalars=scalars,
            retained_files=retained_files,
            validity=validity,
            validity_reasons=validity_reasons,
        )
    )
    await session.flush()
    await audit.record(
        session,
        RecordInput(
            actor="system:runner",
            actor_type="system",
            action="study.result.recorded",
            resource_type=resource_type,
            resource_id=str(source_id),
            outcome="success",
            payload={
                "job_id": job.id,
                "source_kind": source_kind,
                "scalar_count": len(scalars),
                "retained_file_count": len(retained_files),
                "succeeded": succeeded,
                "validity": validity,
            },
        ),
    )


async def backfill_validity(session: AsyncSession) -> tuple[int, int]:
    """Recompute validity for existing ``study_result`` rows (ADR-0017 backfill).

    For each row, re-evaluates :func:`_evaluate_validity` from the row's stored
    scalars + the member job's current tool descriptor, and updates ``validity``
    / ``validity_reasons`` when they change. Field per-node bounds are NOT
    re-applied here (snapshots may be gone); ingest handles those live, and a
    field run's companion scalar (e.g. ``fi_max``) still flags it from stored
    scalars. Returns ``(n_scanned, n_updated)``. Rows whose tool no longer
    exists are skipped (no descriptor to judge against).
    """
    rows = (
        await session.execute(
            select(StudyResult, Tool)
            .join(Job, Job.id == StudyResult.job_id)
            .join(Tool, Tool.alias == Job.tool_alias)
        )
    ).all()
    n_scanned = 0
    n_updated = 0
    for result, tool in rows:
        n_scanned += 1
        validity, reasons = _evaluate_validity(dict(result.scalars or {}), tool)
        changed = validity != (result.validity or "valid") or reasons != list(
            result.validity_reasons or []
        )
        if changed:
            result.validity = validity
            result.validity_reasons = reasons
            n_updated += 1
    await session.flush()
    return n_scanned, n_updated


def _evaluate_validity(scalars: dict[str, Any], tool: Tool) -> tuple[str, list[dict[str, Any]]]:
    """Stamp output validity from a child's scalars vs the descriptor (ADR-0017).

    Every numeric output carries an implicit finiteness floor (``NaN``/``Inf``
    is invalid unless the descriptor sets ``allow_non_finite``) plus optional
    ``min``/``max`` bounds. A field output's validity rides on its companion
    scalar (e.g. ``fi_max`` beside ``failure_field``): bound that scalar and a
    blown-up field run is flagged here **without loading the mesh** — per-node
    field bounds are the separate ingest-time check. Returns
    ``(validity, reasons)`` where ``reasons`` is ``[{output, code, detail}]``
    with codes ``non_finite`` / ``below_min`` / ``above_max``; empty when valid.
    """
    descriptor = tool.descriptor if isinstance(tool.descriptor, dict) else {}
    outputs_decl = descriptor.get("outputs", []) or []
    reasons: list[dict[str, Any]] = []
    if isinstance(outputs_decl, list):
        for decl in outputs_decl:
            if not isinstance(decl, dict) or decl.get("type") != "number":
                continue
            name = str(decl.get("name") or "")
            if not name or name not in scalars or not _is_number(scalars[name]):
                continue
            value = float(scalars[name])
            if not bool(decl.get("allow_non_finite", False)) and not math.isfinite(value):
                reasons.append({"output": name, "code": "non_finite", "detail": f"{name}={value}"})
                continue  # bounds are meaningless on a non-finite value
            lo, hi = decl.get("min"), decl.get("max")
            if lo is not None and value < float(lo):
                reasons.append(
                    {"output": name, "code": "below_min", "detail": f"{name}={value} < min {lo}"}
                )
            if hi is not None and value > float(hi):
                reasons.append(
                    {"output": name, "code": "above_max", "detail": f"{name}={value} > max {hi}"}
                )
    return ("invalid" if reasons else "valid"), reasons


def _extract_scalars(outputs_doc: OutputsDocument) -> dict[str, Any]:
    """Collect numeric + string scalar OutputValues from the document.

    File / table outputs are excluded — they are not scalar response
    axes. ``None`` values are dropped (treated as "the tool didn't
    write this output for this child").
    """
    out: dict[str, Any] = {}
    for o in outputs_doc.outputs:
        if o.type in ("number", "string") and o.value is not None:
            out[o.name] = o.value
    return out


def _extract_retained_files(outputs_doc: OutputsDocument, tool: Tool) -> list[dict[str, Any]]:
    """Reference every file output the run produced, with its kind.

    The cleanup floor (ADR-0003) already keeps ``csv_plot`` files on
    disk; this records the *references* (filename + kind + output
    name) so the Results explorer can find them for drill-down (any
    file) and the response-from-column picker / curve-overlay
    (``csv_plot`` files specifically). The kind mirrors the
    descriptor's ``OutputField.file_type``, falling back to
    ``"file"`` when unset.
    """
    descriptor = tool.descriptor if isinstance(tool.descriptor, dict) else {}
    outputs_decl = descriptor.get("outputs", []) or []
    file_type_by_name: dict[str, str] = {}
    if isinstance(outputs_decl, list):
        for entry in outputs_decl:
            if isinstance(entry, dict) and entry.get("type") == "file":
                file_type_by_name[str(entry.get("name") or "")] = str(entry.get("file_type") or "")
    refs: list[dict[str, Any]] = []
    for o in outputs_doc.outputs:
        if o.type == "file" and o.filename:
            refs.append(
                {
                    "name": o.name,
                    "filename": o.filename,
                    "kind": file_type_by_name.get(o.name) or "file",
                }
            )
    return refs


# ---------------------------------------------------------------------------
# Read side — service for the API + UI
# ---------------------------------------------------------------------------


class ResultsService:
    """Read-side access to the ``parametric_study_result`` store.

    ACL: a study's results inherit its project's ACL — we delegate
    the access check to :meth:`StudyService.get` (project VIEW
    required), which is the same path the Progress tab uses.
    """

    def __init__(self, study_service: StudyService) -> None:
        """Compose over a :class:`StudyService` for the ACL gate."""
        self._studies = study_service

    async def get_results_for_study(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> StudyResultsView:
        """Return the (children x inputs x outputs) matrix for one study.

        Includes every child job — those without a results row yet
        (still running, or pre-b0 children) appear with empty
        ``scalars`` and ``retained_files`` so the UI can still render
        the row.
        """
        study: ParametricStudy = await self._studies.get(
            session, actor_user_id=actor_user_id, study_id=study_id
        )
        # Constrain the join to *this study's* result rows. A job can now
        # carry several ``study_result`` rows — its own ``parametric`` row
        # plus a ``cohort`` row once it joins a Job Cohort (the lazy cohort
        # populate, ADR-0008 §3.3). Joining on ``job_id`` alone fans those
        # out, duplicating every such run in the matrix; pin the source so
        # each job joins at most one row (and a child without a result row
        # still surfaces via the outer join — D3 null-with-banner).
        stmt = (
            select(Job, StudyResult)
            .outerjoin(
                StudyResult,
                and_(
                    StudyResult.job_id == Job.id,
                    StudyResult.source_kind == "parametric",
                    StudyResult.source_id == study.id,
                ),
            )
            .where(Job.parametric_study_id == study.id)
            .order_by(Job.id.asc())
        )
        rows = (await session.execute(stmt)).all()
        children: list[StudyChildResult] = []
        for job, result in rows:
            children.append(
                StudyChildResult(
                    job_id=int(job.id),
                    state=str(job.state),
                    inputs=dict(job.inputs or {}),
                    scalars=dict(result.scalars or {}) if result is not None else {},
                    retained_files=(
                        list(result.retained_files or []) if result is not None else []
                    ),
                    validity=str(result.validity or "valid") if result is not None else "valid",
                    validity_reasons=(
                        list(result.validity_reasons or []) if result is not None else []
                    ),
                )
            )
        return StudyResultsView(
            study_id=int(study.id),
            tool_alias=str(study.tool_alias),
            tool_version_id=int(study.tool_version_id),
            children=children,
        )

    async def latest_sensitivity_weights(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        max_studies: int = 8,
    ) -> tuple[dict[str, float], str] | None:
        """Per-factor sensitivity weights from the tool's most recent study.

        Backs the "sensitivity-weighted when available" half of the
        find-similar ranking (roadmap, extends §16.5). Walks the tool's
        studies newest-first; for the first one the actor can see whose
        results yield standardised regression coefficients it returns
        ``({factor: |SRC|}, source_label)`` where ``source_label`` is a
        short human string for the results-table note (e.g.
        ``"Study 42 — Heat Sink sweep"``).

        Returns ``None`` when no accessible, sufficiently-sampled study
        exists — the caller then ranks with uniform weights. Studies the
        actor can't see are skipped silently (the ACL check lives in
        :meth:`get_results_for_study`), never leaked as an error.
        """
        rows = (
            await session.execute(
                select(ParametricStudy.id, ParametricStudy.name)
                .where(ParametricStudy.tool_alias == tool_alias)
                .order_by(ParametricStudy.id.desc())
                .limit(max(1, int(max_studies)))
            )
        ).all()
        for study_id, study_name in rows:
            try:
                view = await self.get_results_for_study(
                    session, actor_user_id=actor_user_id, study_id=int(study_id)
                )
            except (NotFoundError, AccessDeniedError):
                continue
            weights = _sensitivity_weights_from_children(view.children)
            if weights:
                return weights, f"Study {int(study_id)} — {study_name}"
        return None


def make_results_service(study_service: StudyService) -> ResultsService:
    """Wire up a :class:`ResultsService` over an existing :class:`StudyService`."""
    return ResultsService(study_service)


__all__ = [
    "ResultsService",
    "StudyChildResult",
    "StudyResultsView",
    "backfill_validity",
    "make_results_service",
    "record_study_result",
]
