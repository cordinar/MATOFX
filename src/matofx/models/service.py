"""ModelService — the surrogate Model registry lifecycle (ADR-0008 §10.2).

Phase 5a owns the **ungated** path (personal / project scope, surrogate
§6.3 — those publish directly): fit a scalar surrogate from any study
source via the source-agnostic ingestion (Phase 3), persist it as a
governed, sha256-verified on-disk bundle + a ``surrogate_model`` /
``surrogate_model_version`` pair, and read it back for the picker and the
runtime. Global scope runs the two-gate release flow — that, plus scope
transitions, the surrogate-as-tool synthesis (Phase 6) and the API/UI,
land in the following slices.

ACL inherits the project model (ADR-0002): personal = the authenticated
owner; project = Member+ on the model's project; global read = everyone
(write deferred). Every mutation audits under ``models.model.*`` /
``surrogate.*`` (studies-engine §8).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.approvals import ApprovalRequest, Decision, make_approval_service
from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import ProjectAccessLevel
from matofx.hierarchy.service import _require_project_access
from matofx.models.bundle import (
    combined_field_reference_mesh_path,
    extract_field_geometry,
    field_reference_mesh_path,
    load_combined_bundle,
    load_field_bundle,
    load_scalar_bundle,
    write_combined_bundle,
    write_field_bundle,
    write_scalar_bundle,
)
from matofx.models.errors import ModelConfigError, ModelStateError
from matofx.models.orm import SurrogateModel, SurrogateModelVersion
from matofx.studies.surrogate import Surrogate
from matofx.studies.surrogate.field import (
    FieldSurrogate,
    FieldSurrogateError,
    fit_field_surrogate,
)
from matofx.studies.surrogate.ingest import (
    FactorFilter,
    FieldSpec,
    ResponseSpec,
    collect_field_training_data,
    collect_training_data,
)
from matofx.studies.surrogate.tuning import (
    METHODS,
    TUNING_MODES,
    Config,
    ResultTuner,
    TuningDecision,
    log_eligible_factors,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


_MIN_FIELD_RUNS = 2
"""A POD field surrogate needs at least two runs to find any field variation."""


Progress = Callable[[int, int, str], None]
"""``(done, total, message)`` — a background fit job's progress (ADR-0022 §7)."""


@dataclass(slots=True)
class _Tuned:
    """One scalar result's training data and its evaluated candidates."""

    response: str
    data: Any
    tuner: ResultTuner


@dataclass(frozen=True, slots=True)
class _ScalarFits:
    """The scalar half of a fit: one surrogate and tuning decision per response."""

    surrogates: dict[str, Surrogate]
    n_points: int
    constants: dict[str, Any]
    excluded: dict[str, Any]
    training: Any
    decisions: dict[str, TuningDecision]


@dataclass(frozen=True, slots=True)
class _FitResult:
    """Everything a (combined) fit produces — what ``_add_version`` persists."""

    surrogates: dict[str, Surrogate]
    field_surrogates: dict[str, FieldSurrogate]
    field_formats: dict[str, str]
    reference_meshes: dict[str, Path | None]
    factors: list[dict[str, Any]]
    responses: list[dict[str, Any]]
    quality: dict[str, Any]
    n_points: int
    constants: dict[str, Any]
    excluded: dict[str, Any]
    training: Any = None
    """The ``TrainingData`` the fit consumed, for the coverage report (ADR-0020 §9).

    Kept because a report rebuilt from the fitted surrogates alone would be
    **blind to the failure the feature exists to catch**: a ``Surrogate``'s
    ``training_inputs`` is numeric-only, so a 40-CFRP / 2-GFRP split would be
    invisible — exactly the gap finding 21 closed on the cohort path. ``None``
    only where no scalar response was fitted."""


def _coverage_columns(
    session: AsyncSession,  # noqa: ARG001
    training: Any,
    *,
    tool_alias: str,  # noqa: ARG001
    descriptor: Any = None,
    surrogates: dict[str, Surrogate] | None = None,
    field_surrogates: dict[str, FieldSurrogate] | None = None,
) -> dict[str, Any]:
    """The coverage columns for a new version (ADR-0020 §9), or empty.

    Computed **at fit**, once, so the model record, the per-job report and the
    V&V trail read one answer rather than each recomputing it and drifting
    apart. A refit mints a new version, so this is written once and never
    amended — there is no stale-report path to guard.

    Returns ``{}`` — leaving both columns NULL, meaning *not assessed* — where
    there is no training data to measure: a field-only fit, or a source the
    ingest layer could not describe. NULL is the honest answer there, and the
    surfaces render it as "not assessed" rather than as a verdict.

    Deliberately best-effort: a coverage failure must never fail a fit that
    otherwise succeeded. The verdict is evidence *about* the model, not a gate
    on producing one (§7).
    """
    if training is None:
        return {}
    try:
        from matofx.studies.coverage import (  # noqa: PLC0415
            analyse_design,
            build_envelope,
            build_report,
            resolve_points,
        )

        # The descriptor is resolved by the caller (this runs in a worker thread
        # and cannot await). Without it every axis label is blank — so every
        # coverage surface, including the exported tool's Training data coverage
        # panel, falls back to the internal name ([[feedback_use_labels_not_names]])
        # — and, worse, an integer axis silently degrades to continuous (§3.3).
        # The cohort preview path has always passed it; this one did not.
        points = resolve_points(training, descriptor=descriptor)
        if not points.factor_names:
            return {}
        envelope = build_envelope(points, descriptor=descriptor)
        report = build_report(
            analyse_design(points, envelope),
            envelope=envelope,
            source_kind=str(getattr(training.source_ref, "source_kind", "cohort") or "cohort"),
            computed_at=_utcnow(),
            # §12: the version's report carries **both** passes. Without the
            # per-response half there is no `edge_sigma_threshold`, and the
            # predict-time guard silently degrades to inside/outside on GP
            # fits that could perfectly well have returned `edge`.
            per_response=_response_coverage(points, envelope, surrogates, field_surrogates),
        )
    except Exception:  # a verdict is evidence, never a gate on fitting
        _log.warning("coverage report could not be computed for this fit", exc_info=True)
        return {}
    return {
        "coverage_report_json": report.to_json_obj(),
        "coverage_verdict": report.verdict,
    }


def _response_coverage(
    points: Any,
    envelope: Any,
    surrogates: dict[str, Surrogate] | None,
    field_surrogates: dict[str, FieldSurrogate] | None,
) -> tuple[Any, ...]:
    """Per-response coverage for the fitted responses (ADR-0020 §2.2).

    One entry per response, each carrying its own criterion, verdict and —
    for a GP fit — the ``edge_sigma_threshold`` the predict-time guard reads.
    That threshold is computed **once, here**: recomputing it per prediction
    would cost a training-set evaluation on every call and is impossible on a
    version fitted with ``keep_training_data=False``.
    """
    from matofx.studies.coverage import ResponseModel, analyse_responses  # noqa: PLC0415

    models: dict[str, ResponseModel] = {
        name: ResponseModel.scalar(name, surrogate)
        for name, surrogate in (surrogates or {}).items()
    }
    models.update(
        {
            name: ResponseModel.field(name, surrogate)
            for name, surrogate in (field_surrogates or {}).items()
        }
    )
    if not models:
        return ()
    return analyse_responses(models, envelope, points=points)


def _field_response_names(version: SurrogateModelVersion) -> list[str]:
    """Names of the version's field outputs (``field`` has one, ``combined`` 0+)."""
    return [
        str(r.get("name"))
        for r in (version.responses or [])
        if r.get("kind") == "field" and r.get("name")
    ]


def _field_support(
    version: SurrogateModelVersion,
    response_name: str,
    surrogate: FieldSurrogate,
    point: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The ADR-0020 §8 support verdict for one field prediction, or ``None``.

    **The viewer is a live prediction and had none.** §8's guard reached users
    only through an exported tool's job outputs; the predicted-field viewer is
    the other place a prediction happens, and the one where somebody is
    actively dragging sliders — which is exactly how you leave the region the
    runs covered without noticing. Pushing ``time`` past the last trained
    snapshot is the textbook case, and it went unremarked.

    The sigma is the response's **energy-weighted relative** sigma across its
    mode surrogates (§2.2.1) — the same quantity whose distribution set the
    stored ``edge`` threshold, so comparing them is meaningful. The threshold
    is read for *this* response, since the viewer knows which one it is
    reconstructing.

    Best-effort by construction: a guard that could raise would turn a caveat
    into a broken viewer, so anything unexpected yields ``None`` (rendered as
    no badge) rather than an exception.
    """
    stored = version.coverage_report_json
    if not stored:
        return None
    try:
        from matofx.studies.coverage import (  # noqa: PLC0415
            CoverageReport,
            edge_threshold_from,
            evaluate_support,
        )
        from matofx.studies.coverage.gp import ResponseModel, relative_sigma  # noqa: PLC0415

        report = CoverageReport.from_json_obj(stored)
        sigmas = relative_sigma(ResponseModel.field(response_name, surrogate), [dict(point)])
        sigma = float(sigmas[0]) if sigmas else None
        support = evaluate_support(
            report.envelope,
            point,
            sigma=sigma,
            edge_threshold=edge_threshold_from(stored, response_name=response_name),
        )
    except Exception:  # a guard must never break the view it is annotating
        _log.warning("support could not be evaluated for this field prediction", exc_info=True)
        return None
    return support.to_json_obj()


def _resolve_field_response(version: SurrogateModelVersion, response_name: str | None) -> str:
    """Pick which field output to act on; default to the first (ADR-0018)."""
    names = _field_response_names(version)
    if not names:
        raise ModelConfigError("This model version has no field output.")
    if response_name is None:
        return names[0]
    if response_name not in names:
        raise ModelConfigError(f"'{response_name}' is not a field output of this model.")
    return response_name


def _load_field(
    version: SurrogateModelVersion, response_name: str
) -> tuple[FieldSurrogate, Path | None]:
    """Sync: sha-verified load of one field surrogate + its reference mesh path.

    Handles ``field`` (single ``field/`` bundle) and ``combined`` (the
    ``field/<response>/`` subtree) versions (ADR-0018).
    """
    bundle_dir = Path(version.bundle_path)
    if version.kind == "field":
        return load_field_bundle(bundle_dir, version.bundle_sha256), field_reference_mesh_path(
            bundle_dir
        )
    if version.kind == "combined":
        _scalars, fields = load_combined_bundle(bundle_dir, version.bundle_sha256)
        return fields[response_name], combined_field_reference_mesh_path(bundle_dir, response_name)
    raise ModelConfigError("This model version has no field output.")


_log = logging.getLogger(__name__)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    """A loaded, integrity-checked model ready to predict (§7.2 runtime entry)."""

    responses: list[str]
    _surrogates: dict[str, Surrogate]

    def predict(self, point: Mapping[str, float]) -> dict[str, float]:
        """Predict every response at one ``{factor: value}`` point."""
        out: dict[str, float] = {}
        for response, surrogate in self._surrogates.items():
            means, _ = surrogate.predict([point])
            out[response] = means[0] if means else 0.0
        return out

    def predict_std(self, point: Mapping[str, float]) -> dict[str, float | None]:
        """Per-response 1-sigma at one point (``None`` for a non-GP backend)."""
        out: dict[str, float | None] = {}
        for response, surrogate in self._surrogates.items():
            _, sigma = surrogate.predict([point])
            out[response] = sigma[0] if sigma else None
        return out


@dataclass(frozen=True, slots=True)
class FieldPredictionResult:
    """A field surrogate's reconstructed field at one point (viewer payload, §4.5)."""

    version_id: int
    n_nodes: int
    field: list[float]
    field_std: list[float] | None
    mode_coefficients: list[float]
    has_geometry: bool
    support: dict[str, Any] | None = None
    """The ADR-0020 §8 support verdict at this point, or ``None``.

    ``None`` means the version predates coverage and has nothing to say —
    which the viewer renders as *no badge*, never as a clean bill of health."""


@dataclass(frozen=True, slots=True)
class FieldGeometryResult:
    """A field version's reference-mesh surface for the 3-D viewer (§4.5).

    ``points`` are the full node coordinates (a per-node field aligns by
    index); ``i``/``j``/``k`` are the surface triangles. ``has_geometry`` is
    ``False`` for ``.npy`` fields (no mesh) or an unalignable surface.
    """

    version_id: int
    has_geometry: bool
    points: list[list[float]]
    i: list[int]
    j: list[int]
    k: list[int]


_MAX_FIELD_PREDICT_NODES = 2_000_000
"""Upper bound on nodes the interactive field-predict endpoint will serialise.

A few-thousand-node demo mesh is ~tens of KB; multi-million-node CFD meshes
are out of scope for the live browser viewer (downsampling is a v1.x item),
so the endpoint refuses them with a plain-English message rather than pushing
hundreds of MB over the wire."""


class ModelService:
    """Lifecycle for the surrogate Model registry (Phase 5a core)."""

    def __init__(self, audit: AuditWriter, *, models_root: Path, environment: str) -> None:
        """Compose over the audit writer + the bundle storage root."""
        self.audit = audit
        self.models_root = Path(models_root)
        self.environment = environment

    # --- create / refit --------------------------------------------------

    async def create_model(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        tool_alias: str,
        scope: str,
        source_kind: str,
        source_id: int,
        response_names: Sequence[str],
        project_id: int | None = None,
        factor_filter: FactorFilter | None = None,
        surrogate_kind: str = "gp",
        description: str = "",
        tags: Sequence[str] | None = None,
        keep_training_data: bool = False,
        is_curator: bool = False,
        field_spec: FieldSpec | None = None,
        field_specs: Sequence[FieldSpec] | None = None,
        field_auto_select: bool = False,
        field_surrogate_kind: str | None = None,
        tuning_mode: str = "off",
        candidate_methods: Sequence[str] | None = None,
        tuning_overrides: Mapping[str, Mapping[str, str]] | None = None,
        progress: Progress | None = None,
    ) -> tuple[SurrogateModel, SurrogateModelVersion, dict[str, Any]]:
        """Fit + persist a new model (ordinal 1). Returns (model, version, excluded).

        Personal models are owned by the caller; project models need
        Member+ on ``project_id`` — both publish directly (§6.3). A
        ``global`` model needs the Model Curator capability
        (``is_curator``, caller-resolved from ``MODELS_CURATE``) and lands
        as a ``pending`` model with a ``draft`` version that the builder
        then submits for the two-gate review (:meth:`submit_for_review`).

        ``response_names`` are scalar outputs; ``field_specs`` (ADR-0018) are
        field outputs fitted as POD surrogates. Supplying both makes a
        ``combined`` version that predicts numbers **and** fields. ``field_spec``
        (singular) is a back-compat shim for the single-field caller. Each
        output trains on its own valid subset (ADR-0017).

        ``tuning_mode`` (``automatic`` / ``off``), the ``candidate_methods`` and
        the per-result ``tuning_overrides`` (``{result: {method, output_scale,
        input_scale}}``) decide how each scalar result is fitted (ADR-0022);
        ``surrogate_kind`` is the primary method every result starts from. The
        choice and its evidence are recorded on the version's ``responses`` and
        ``quality``.
        """
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ModelConfigError("A model needs a name.")
        specs = list(field_specs or [])
        if field_spec is not None:
            specs.append(field_spec)
        # A response named by a field spec is a field, not a scalar — drop it
        # from the scalar list so the back-compat single-field caller (which
        # passes the field name in both response_names and field_spec) still
        # fits one field, not a phantom scalar.
        field_names = {s.response_name for s in specs}
        scalar_names = [r for r in response_names if r not in field_names]
        if not scalar_names and not specs:
            raise ModelConfigError("Pick at least one response (scalar or field) to model.")
        project_id = await self._authorise_scope(
            session, actor_user_id, scope, project_id, is_curator
        )

        fit = await self._fit_all(
            session,
            source_kind=source_kind,
            source_id=source_id,
            response_names=scalar_names,
            field_specs=specs,
            factor_filter=factor_filter,
            surrogate_kind=surrogate_kind,
            field_auto_select=field_auto_select,
            field_surrogate_kind=field_surrogate_kind,
            tool_alias=tool_alias,
            tuning_mode=tuning_mode,
            candidate_methods=candidate_methods,
            tuning_overrides=tuning_overrides,
            progress=progress,
        )
        tv_ids = [await self._latest_tool_version_id(session, tool_alias)]

        is_global = scope == "global"
        model = SurrogateModel(
            name=cleaned_name,
            description=description.strip(),
            tags=list(tags or []),
            tool_alias=tool_alias.strip(),
            scope=scope,
            project_id=project_id,
            owner_user_id=actor_user_id,
            state="pending" if is_global else "active",
            created_by_user_id=actor_user_id,
        )
        session.add(model)
        await session.flush()
        version = await self._add_version(
            session,
            model=model,
            ordinal=1,
            actor_user_id=actor_user_id,
            surrogates=fit.surrogates,
            field_surrogates=fit.field_surrogates,
            factors=fit.factors,
            responses=fit.responses,
            quality=fit.quality,
            source_kind=source_kind,
            source_id=source_id,
            tool_version_ids=tv_ids,
            n_points=fit.n_points,
            keep_training_data=keep_training_data,
            published=not is_global,
            input_constants=fit.constants,
            surrogate_method=surrogate_kind,
            field_formats=fit.field_formats,
            reference_meshes=fit.reference_meshes,
            training=fit.training,
        )
        excluded = fit.excluded
        await self._audit(
            session,
            "models.model.create",
            actor_user_id,
            model.id,
            payload={
                "name": model.name,
                "tool_alias": model.tool_alias,
                "scope": scope,
                "version_id": version.id,
                "responses": list(response_names),
                "excluded": excluded,
                "tuning": _tuning_audit(fit.responses),
            },
        )
        return model, version, excluded

    async def refit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        model_id: int,
        factor_filter: FactorFilter | None = None,
        surrogate_kind: str | None = None,
        tuning_mode: str | None = None,
        progress: Progress | None = None,
    ) -> SurrogateModelVersion:
        """Re-fit on the source's current data → a new version (§8).

        Re-runs the ingestion + fit with the same response set + source as
        the latest version, mints the next ordinal, and (personal /
        project) republishes to it. The fitting method is inherited from the
        latest version unless ``surrogate_kind`` overrides it; the per-result
        tuning setting likewise unless ``tuning_mode`` does (ADR-0022). The
        user's own per-result choices are kept; MATOFX's picks are re-decided.
        """
        model = await self._get_with_access(session, model_id, actor_user_id, write=True)
        latest = await self._latest_version(session, model_id)
        if latest is None:
            raise ModelStateError("This model has no version to refit from.")
        method = surrogate_kind or latest.surrogate_method or "gp"
        # Scalar responses + field specs come from the latest version's response
        # list, so a refit reproduces the same mixed shape (ADR-0018).
        response_names = [
            str(r.get("name"))
            for r in (latest.responses or [])
            if r.get("name") and r.get("kind") != "field"
        ]
        field_specs = _field_specs_from_version(latest)
        # Reproduce the field's original method (ADR-0018): prefer the recorded
        # per-response ``field_method`` (incl 'auto'); fall back to the legacy
        # ``auto_select`` flag for versions fitted before it was recorded.
        field_methods = [
            str(r.get("field_method"))
            for r in (latest.responses or [])
            if r.get("kind") == "field" and r.get("field_method")
        ]
        field_surrogate_kind = field_methods[0] if field_methods else None
        field_auto_select = any(
            r.get("kind") == "field" and r.get("auto_select") for r in (latest.responses or [])
        )
        recorded = _recorded_tuning(latest.responses or [], method)
        tuning_mode = tuning_mode or recorded.mode

        fit = await self._fit_all(
            session,
            source_kind=latest.source_kind,
            source_id=latest.source_id,
            response_names=response_names,
            field_specs=field_specs,
            factor_filter=factor_filter,
            surrogate_kind=method,
            field_auto_select=field_auto_select,
            field_surrogate_kind=field_surrogate_kind,
            tool_alias=model.tool_alias,
            tuning_mode=tuning_mode,
            candidate_methods=recorded.methods,
            tuning_overrides=recorded.overrides,
            progress=progress,
        )
        version = await self._add_version(
            session,
            model=model,
            ordinal=latest.ordinal + 1,
            actor_user_id=actor_user_id,
            surrogates=fit.surrogates,
            field_surrogates=fit.field_surrogates,
            factors=fit.factors,
            responses=fit.responses,
            quality=fit.quality,
            source_kind=latest.source_kind,
            source_id=latest.source_id,
            tool_version_ids=list(latest.tool_version_ids_at_fit or []),
            n_points=fit.n_points,
            keep_training_data=latest.keep_training_data,
            published=model.scope != "global",
            input_constants=fit.constants,
            surrogate_method=method,
            field_formats=fit.field_formats,
            reference_meshes=fit.reference_meshes,
            training=fit.training,
        )
        await self._audit(
            session,
            "surrogate.refit",
            actor_user_id,
            model_id,
            payload={
                "version_id": version.id,
                "ordinal": version.ordinal,
                "n_points": fit.n_points,
                "tuning": _tuning_audit(fit.responses),
            },
        )
        return version

    # --- read ------------------------------------------------------------

    async def get(
        self, session: AsyncSession, *, actor_user_id: int, model_id: int
    ) -> SurrogateModel:
        """Fetch a model (scope-resolved read access, §6.1)."""
        return await self._get_with_access(session, model_id, actor_user_id, write=False)

    async def list_for_picker(
        self, session: AsyncSession, *, actor_user_id: int, tool_alias: str
    ) -> list[SurrogateModel]:
        """Models for ``tool_alias`` the caller may pick (§6.1), retired hidden."""
        stmt = (
            select(SurrogateModel)
            .where(SurrogateModel.tool_alias == tool_alias.strip())
            .where(SurrogateModel.state != "retired")
            .order_by(SurrogateModel.created_at.desc(), SurrogateModel.id.desc())
        )
        visible: list[SurrogateModel] = []
        for model in (await session.execute(stmt)).scalars().all():
            # A pending / un-published global model is not pickable — it
            # surfaces only in the curator's approval inbox (§6.1).
            if model.scope == "global" and model.published_version_id is None:
                continue
            if await self._can_read(session, model, actor_user_id):
                visible.append(model)
        return visible

    async def list_models(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str | None = None,
        scope: str | None = None,
        include_retired: bool = False,
    ) -> list[SurrogateModel]:
        """All models the caller may read (§6.1) for the ``/models`` catalogue.

        Unlike :meth:`list_for_picker`, this surfaces the caller's own
        in-progress models — personal / project drafts and un-published
        global pending entries — so the workspace can show them with a
        status badge. Retired models are hidden unless ``include_retired``.
        """
        stmt = select(SurrogateModel).order_by(
            SurrogateModel.created_at.desc(), SurrogateModel.id.desc()
        )
        if tool_alias:
            stmt = stmt.where(SurrogateModel.tool_alias == tool_alias.strip())
        if scope:
            stmt = stmt.where(SurrogateModel.scope == scope)
        if not include_retired:
            stmt = stmt.where(SurrogateModel.state != "retired")
        visible: list[SurrogateModel] = []
        for model in (await session.execute(stmt)).scalars().all():
            if await self._can_read(session, model, actor_user_id):
                visible.append(model)
        return visible

    async def coverage_for_version(
        self, session: AsyncSession, *, actor_user_id: int, version_id: int
    ) -> list[dict[str, Any]]:
        """The frozen training-range factors of a version (the §7.3 gauge source).

        Returns ``[{name, min, max, unit?}]`` — the data the in-form
        coverage gauge classifies the user's inputs against. Scope-read
        access-checked (existence-hidden on no access) like :meth:`get`.
        """
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        await self._get_with_access(session, version.surrogate_model_id, actor_user_id, write=False)
        return [dict(f) for f in (version.factors or [])]

    async def coverage_report_for_version(
        self, session: AsyncSession, *, actor_user_id: int, version_id: int
    ) -> Any:
        """The stored design-space coverage report, or ``None`` (ADR-0020 §9).

        Deliberately **not** named ``coverage_for_version``: that already
        exists and means the ADR-0008 §7.3 in-form gauge — the frozen
        training *ranges* a user's inputs are classified against. Two
        different things are called coverage in this codebase, and merging
        them would silently change what one of the surfaces shows.

        ``None`` means *not assessed*: either the version predates this
        feature or its report could not be computed. Callers render that as
        "not assessed" and **never** as "good" — silence about coverage and a
        clean bill of health are opposite claims.
        """
        from matofx.studies.coverage import CoverageReport  # noqa: PLC0415

        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        await self._get_with_access(session, version.surrogate_model_id, actor_user_id, write=False)
        stored = version.coverage_report_json
        if not stored:
            return None
        return CoverageReport.from_json_obj(stored)

    async def resolve_field_spec(
        self, session: AsyncSession, *, tool_alias: str, response_name: str
    ) -> FieldSpec | None:
        """Build a :class:`FieldSpec` from a tool's ``field`` output, or ``None``.

        Reads the latest tool-version descriptor and returns the spec for the
        named output when it is ``file_type == "field"`` — the API uses this
        to route a create request to the field path without a schema change
        (the descriptor stays authoritative). A scalar output returns ``None``.
        """
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        snapshot = (
            await session.execute(
                select(ToolVersion.descriptor_snapshot)
                .where(ToolVersion.alias == tool_alias.strip())
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        outputs = (snapshot or {}).get("outputs") or []
        for out in outputs:
            if (
                isinstance(out, dict)
                and out.get("name") == response_name
                and out.get("file_type") == "field"
            ):
                return FieldSpec(
                    response_name=response_name,
                    file_format=str(out.get("field_format") or "npy"),
                    node_array_name=out.get("field_node_array_name"),
                    timesteps_glob=out.get("field_timesteps"),
                    value_min=out.get("value_min"),
                    value_max=out.get("value_max"),
                )
        return None

    async def field_predict(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        version_id: int,
        point: Mapping[str, Any],
        response_name: str | None = None,
    ) -> FieldPredictionResult:
        """Reconstruct a field version's nodal field at one point (§4.5 viewer).

        Scope-read access-checked. The bundle is sha-verified and loaded off
        the event loop (``to_thread``) — a trusted curator preview, not tool
        execution, so it runs in the web process rather than a run subprocess.
        Refuses non-field versions and meshes too large for an interactive
        frame.
        """
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        await self._get_with_access(session, version.surrogate_model_id, actor_user_id, write=False)
        if version.kind not in ("field", "combined"):
            raise ModelConfigError("This model version is not a field surrogate.")
        resolved = _resolve_field_response(version, response_name)
        surrogate, mesh_path = await asyncio.to_thread(_load_field, version, resolved)
        if surrogate.n_nodes > _MAX_FIELD_PREDICT_NODES:
            raise ModelConfigError(
                f"This field has {surrogate.n_nodes:,} nodes — too large for the "
                "interactive viewer. (Downsampled field preview is a later feature.)"
            )
        prediction = surrogate.predict(point)
        field_std = (
            [float(v) for v in prediction.field_std.tolist()]
            if prediction.field_std is not None
            else None
        )
        return FieldPredictionResult(
            version_id=version_id,
            n_nodes=surrogate.n_nodes,
            field=[float(v) for v in prediction.field.tolist()],
            field_std=field_std,
            mode_coefficients=[float(v) for v in prediction.mode_coefficients],
            has_geometry=mesh_path is not None,
            support=_field_support(version, resolved, surrogate, point),
        )

    async def field_geometry(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        version_id: int,
        response_name: str | None = None,
    ) -> FieldGeometryResult:
        """The reference-mesh surface for a field version's 3-D viewer (§4.5).

        Scope-read access-checked. Returns an empty (``has_geometry=False``)
        result for a ``.npy`` field (no mesh) so the viewer falls back to the
        value-vs-node line; the surface extraction runs off the event loop.
        Refuses a non-field version.
        """
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        await self._get_with_access(session, version.surrogate_model_id, actor_user_id, write=False)
        if version.kind not in ("field", "combined"):
            raise ModelConfigError("This model version is not a field surrogate.")
        empty = FieldGeometryResult(
            version_id=version_id, has_geometry=False, points=[], i=[], j=[], k=[]
        )
        resolved = _resolve_field_response(version, response_name)
        if version.kind == "combined":
            mesh_path = combined_field_reference_mesh_path(Path(version.bundle_path), resolved)
        else:
            mesh_path = field_reference_mesh_path(Path(version.bundle_path))
        if mesh_path is None:
            return empty
        geometry = await asyncio.to_thread(extract_field_geometry, mesh_path)
        if geometry is None:
            return empty
        points, i, j, k = geometry
        return FieldGeometryResult(
            version_id=version_id, has_geometry=True, points=points, i=i, j=j, k=k
        )

    async def open_bundle(
        self, session: AsyncSession, *, actor_user_id: int, version_id: int
    ) -> tuple[str, str]:
        """Access-checked ``(bundle_path, sha256)`` for a version (§10.2)."""
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        await self._get_with_access(session, version.surrogate_model_id, actor_user_id, write=False)
        return version.bundle_path, version.bundle_sha256

    def load_for_runtime(
        self, version: SurrogateModelVersion, expected_sha256: str
    ) -> ModelRuntime:
        """Load + sha256-verify a version's bundle for prediction (§7.2).

        Used by the synthesised orchestrator (Phase 6) — a runner-subprocess
        path, never the web process (non-negotiable #9). Raises
        :class:`~matofx.models.errors.ModelBundleCorruptError` on mismatch.
        """
        surrogates = load_scalar_bundle(Path(version.bundle_path), expected_sha256)
        response_names = [str(r.get("name")) for r in (version.responses or []) if r.get("name")]
        return ModelRuntime(responses=response_names, _surrogates=surrogates)

    async def update_metadata(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        model_id: int,
        name: str | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> SurrogateModel:
        """Edit a model's mutable entry-level metadata (optimistic-locked)."""
        model = await self._get_with_access(session, model_id, actor_user_id, write=True)
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise ModelConfigError("A model needs a name.")
            model.name = cleaned
        if description is not None:
            model.description = description.strip()
        if tags is not None:
            model.tags = list(tags)
        await session.flush()
        await self._audit(
            session,
            "models.model.update",
            actor_user_id,
            model_id,
            payload={"fields": sorted(_changed_fields(name, description, tags))},
        )
        return model

    async def set_scope(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        model_id: int,
        new_scope: str,
        project_id: int | None = None,
    ) -> SurrogateModel:
        """Move a model between scope tiers (§6.4) — ungated transitions only (5b).

        ``personal → project`` needs Member+ on the destination project;
        ``project → personal`` needs write on the model. Anything touching
        ``global`` runs the two-gate release / admin override (governance
        slice) and is rejected here with a clear message.
        """
        model = await self._get_with_access(session, model_id, actor_user_id, write=True)
        if new_scope == model.scope:
            return model
        if new_scope == "global" or model.scope == "global":
            raise ModelStateError(
                "Moving a model to or from global scope isn't available yet — "
                "the release gate lands in the next release."
            )
        if new_scope == "project":
            if project_id is None:
                raise ModelConfigError("A project-scoped model needs a destination project.")
            await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.EDIT,
            )
            model.scope = "project"
            model.project_id = project_id
        elif new_scope == "personal":
            model.scope = "personal"
            model.project_id = None
        else:
            raise ModelConfigError(f"Unknown model scope '{new_scope}'.")
        await session.flush()
        await self._audit(
            session,
            "models.model.scope.change",
            actor_user_id,
            model_id,
            payload={"scope": new_scope, "project_id": model.project_id},
        )
        return model

    async def retire(
        self, session: AsyncSession, *, actor_user_id: int, model_id: int
    ) -> SurrogateModel:
        """Retire a model — withdrawn from pickers, never hard-deleted (D20)."""
        model = await self._get_with_access(session, model_id, actor_user_id, write=True)
        if model.state == "retired":
            return model
        model.state = "retired"
        await session.flush()
        await self._audit(session, "models.model.retire", actor_user_id, model_id, payload={})
        return model

    # --- two-gate global release (§6.2/§6.3) -----------------------------

    async def submit_for_review(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        version_id: int,
        approval_required: bool,
        checker_user_id: int | None = None,
        checker_username: str | None = None,
        builder_is_curator: bool = False,
    ) -> SurrogateModelVersion:
        """Submit a ``draft`` global version into the publish gate (gate 1, §6.3).

        ``approval_required`` is the caller-resolved
        ``models_global_publish_approval`` toggle. When ON, the builder
        nominates one ``checker`` (a ``MODELS_CURATE`` holder or su/admin,
        never the builder — self-nomination is rejected by the approvals
        layer): the version goes ``in_review`` and a directed approval
        request is opened. When OFF, the version skips to ``checked``
        (release-pending) but the builder must themselves hold
        ``MODELS_CURATE`` (``builder_is_curator``).
        """
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        model = await self._get_with_access(
            session, version.surrogate_model_id, actor_user_id, write=True
        )
        if model.scope != "global":
            raise ModelStateError("Only a global model version goes through the publish gate.")
        if version.publish_state != "draft":
            raise ModelStateError(
                f"This version is {version.publish_state!r}; only a draft can be "
                "submitted for review."
            )
        if approval_required:
            username = (checker_username or "").strip()
            if checker_user_id is None or not username:
                raise ModelConfigError("Nominate a checker (a Model Curator) for the review.")
            version.publish_state = "in_review"
            await session.flush()
            await make_approval_service(self.audit).create_request(
                session,
                surrogate_model_version_id=version.id,
                kind="pre",
                requested_by_user_id=actor_user_id,
                eligible_approvers=[username],
                designated_approver_user_id=checker_user_id,
            )
        else:
            if not builder_is_curator:
                raise ModelStateError(
                    "Publishing a global model directly needs the Model Curator capability."
                )
            version.publish_state = "checked"
            await session.flush()
        await self._audit(
            session,
            "models.model.submit_review",
            actor_user_id,
            model.id,
            payload={"version_id": version.id, "approval_required": approval_required},
        )
        return version

    async def decide_check(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Decision,
        reason: str | None = None,
        decider_is_curator: bool,
        decider_is_admin: bool,
    ) -> tuple[ApprovalRequest, SurrogateModelVersion]:
        """The nominated checker approves / rejects the technical sign-off (gate 1).

        Delegates the decision + self-approval / eligibility defence to
        :class:`ApprovalService`, then applies the model-side effect:
        approve → version ``checked`` (release-pending); reject →
        ``rejected``. ``decider_is_curator`` is the caller-resolved
        ``MODELS_CURATE`` fact for the directed gate.
        """
        request = await make_approval_service(self.audit).decide(
            session,
            request_id=request_id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,
            reason=reason,
            decider_is_approver=decider_is_curator,
            decider_is_admin=decider_is_admin,
        )
        if request.surrogate_model_version_id is None:
            raise ModelStateError("That approval request isn't for a surrogate model version.")
        version = await session.get(SurrogateModelVersion, request.surrogate_model_version_id)
        if version is None:
            raise NotFoundError("surrogate model version", request.surrogate_model_version_id)
        if request.status == "approved":
            version.publish_state = "checked"
            action = "models.model.check_approve"
        else:
            version.publish_state = "rejected"
            action = "models.model.check_reject"
        await session.flush()
        await self._audit(
            session,
            action,
            decider_user_id,
            version.surrogate_model_id,
            payload={"version_id": version.id},
        )
        return request, version

    async def release_version(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        version_id: int,
        is_admin: bool,
    ) -> SurrogateModelVersion:
        """Su/admin release of a ``checked`` version to general use (gate 2, §6.3).

        The mandatory, non-toggleable second gate: only a super-user /
        admin (``is_admin``, caller-resolved) flips ``checked → published``,
        advances the model's published pointer, and activates the model.
        """
        if not is_admin:
            raise ModelStateError(
                "Releasing a model to general use is a super-user / admin action."
            )
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise NotFoundError("surrogate model version", version_id)
        if version.publish_state != "checked":
            raise ModelStateError(
                f"This version is {version.publish_state!r}; only a checked version "
                "can be released."
            )
        model = await session.get(SurrogateModel, version.surrogate_model_id)
        if model is None:
            raise NotFoundError("surrogate model", version.surrogate_model_id)
        version.publish_state = "published"
        model.published_version_id = version.id
        model.state = "active"
        await session.flush()
        await self._audit(
            session,
            "models.model.release",
            actor_user_id,
            model.id,
            payload={"version_id": version.id},
        )
        return version

    # --- internals -------------------------------------------------------

    async def _authorise_scope(
        self,
        session: AsyncSession,
        actor_user_id: int,
        scope: str,
        project_id: int | None,
        is_curator: bool,
    ) -> int | None:
        if scope == "global":
            if not is_curator:
                raise ModelStateError(
                    "Creating a global surrogate model needs the Model Curator "
                    "capability (models.curate)."
                )
            return None
        if scope == "project":
            if project_id is None:
                raise ModelConfigError("A project-scoped model needs a project.")
            await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.EDIT,
            )
            return project_id
        if scope == "personal":
            return None
        raise ModelConfigError(f"Unknown model scope '{scope}'.")

    async def compare_fits(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        source_kind: str,
        source_id: int,
        response_names: Sequence[str],
        methods: Sequence[str],
        factor_filter: FactorFilter | None = None,
        tool_alias: str | None = None,
        tuning_mode: str = "off",
        progress: Progress | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], dict[str, Any], dict[str, Any]]:
        """Fit each method over the same data, persisting nothing (the bake-off).

        Returns ``(per_method, best_overall, best_per_response, excluded, tuning)``.
        ``excluded`` is the ADR-0017 ``{n_excluded, reason_counts}`` summary
        (identical across methods — validity is row-level). ``per_method``
        carries each method's cross-validated quality + held-out ``cv_actual`` /
        ``cv_predicted`` pairs (so the UI can overlay parity plots) and a
        ``mean_q2`` aggregate for what **starting from that method** gives — each
        result after tuning (ADR-0022) — with the before-tuning figures (the method
        exactly as measured) in ``baseline_quality`` / ``baseline_mean_q2``.
        ``best_overall`` — the suggested starting method — is the one with the
        highest **before-tuning** mean Q² (Q² is unitless, so it aggregates across
        responses; RMSE does not). A method
        that fails to fit (e.g. an under-determined polynomial) is reported
        ``fitted=False`` with a message
        rather than aborting the whole comparison. No scope/persistence — the
        caller creates the model with the chosen method afterwards.

        ``tuning`` (ADR-0022) is ``{result: {"candidates": [...], "decisions":
        {method: decision}}}`` — every evaluated configuration and what MATOFX
        would pick for each possible primary method, so the page can re-render
        when the user changes the kept method without refitting. Candidates are
        evaluated once per result and shared by every method's decision.
        """
        del actor_user_id  # read of the source mirrors create_model (no extra ACL)
        methods = _validated_methods(methods)
        minimums = await self._declared_minimums(session, tool_alias) if tool_alias else None
        labels = await self._output_labels(session, tool_alias) if tool_alias else {}
        tuned = await self._tune_responses(
            session,
            source_kind,
            source_id,
            response_names,
            factor_filter,
            methods=methods,
            tuning_mode=tuning_mode,
            minimums=minimums,
            labels=labels,
            progress=progress,
        )
        excluded: dict[str, Any] = {"n_excluded": 0, "reason_counts": {}}
        for t in tuned:
            excluded = _larger_exclusion(excluded, t.data)

        if progress is not None and tuned:
            n_fits = sum(len(t.tuner.planned()) for t in tuned)
            progress(n_fits, n_fits, "Checking which combinations hold up in repeated tests")

        def _decide_all() -> tuple[
            dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]
        ]:
            by_method: dict[str, dict[str, Any]] = {m: {} for m in methods}
            tuned_by_method: dict[str, dict[str, Any]] = {m: {} for m in methods}
            tuning: dict[str, Any] = {}
            for t in tuned:
                decisions = {}
                for m in methods:
                    baseline, _ = t.tuner.baseline(m)  # type: ignore[arg-type]
                    base_fit = t.tuner.fit(baseline)
                    by_method[m][t.response] = base_fit
                    tuned_by_method[m][t.response] = base_fit
                    if base_fit.fitted:
                        chosen, decision = t.tuner.decide(m)  # type: ignore[arg-type]
                        tuned_by_method[m][t.response] = chosen
                        decisions[m] = decision.to_json()
                tuning[t.response] = {
                    # Held-out predictions ride along so the page can plot every
                    # fit tried, not only the one used (compare only — not stored).
                    "candidates": [
                        c.to_json() | _cv_pairs(t.tuner.fit(c.config)) for c in t.tuner.scores()
                    ],
                    "decisions": decisions,
                }
            return by_method, tuned_by_method, tuning

        by_method, tuned_by_method, tuning = await asyncio.to_thread(_decide_all)
        per_method: list[dict[str, Any]] = []
        for method in methods:
            fits = by_method[method]
            failed = next((r for r, f in fits.items() if not f.fitted), None)
            if failed is not None:
                per_method.append(
                    {
                        "method": method,
                        "fitted": False,
                        "message": f"Couldn't fit a surrogate for '{failed}': "
                        f"{fits[failed].quality.message or 'not enough complete runs'}",
                        "quality": {},
                    }
                )
                continue
            # What the page ranks methods on is what keeping that method would
            # actually give — each result after tuning (ADR-0022). The pre-tuning
            # figures ride along, so the page can show "before" beside "after";
            # ranking on them made a method that tunes well look second best.
            tuned_fits = tuned_by_method[method]
            per_method.append(
                {
                    "method": method,
                    "fitted": True,
                    "message": None,
                    "quality": {r: _quality_row(f, None) for r, f in tuned_fits.items()},
                    "mean_q2": _mean_q2(tuned_fits.values()),
                    "baseline_quality": {r: _quality_row(f, None) for r, f in fits.items()},
                    "baseline_mean_q2": _mean_q2(fits.values()),
                }
            )

        # The suggested starting method is ranked **before** tuning. Ranked after
        # it, every method whose results all moved elsewhere tied at the top — a
        # "best" polynomial none of whose results was fitted by a polynomial.
        fitted = [m for m in per_method if m["fitted"] and m.get("baseline_mean_q2") is not None]
        best_overall = (
            max(fitted, key=lambda m: m["baseline_mean_q2"])["method"] if fitted else None
        )
        best_per_response: dict[str, str] = {}
        for resp in response_names:
            scored = [
                (m["method"], m["baseline_quality"].get(resp, {}).get("q2"))
                for m in per_method
                if m["fitted"]
            ]
            scored = [(name, q2) for name, q2 in scored if q2 is not None]
            if scored:
                best_per_response[str(resp)] = max(scored, key=lambda c: c[1])[0]
        return per_method, best_overall, best_per_response, excluded, tuning

    async def compare_field_fits(
        self,
        session: AsyncSession,
        *,
        source_kind: str,
        source_id: int,
        field_specs: Sequence[FieldSpec],
        methods: Sequence[str],
        factor_filter: FactorFilter | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
        """Bake off field-surrogate methods per field response (ADR-0018), no persistence.

        Each field's snapshots are collected **once** (loading is the expensive
        part), then every method — including ``'auto'`` (best regressor per POD
        mode) — is fitted on the same data. Returns
        ``(field_per_method, best_field_overall, excluded)``: per method, the
        per-response Field Q² (energy-weighted), reconstruction summary, per-mode
        Q², and the held-out mean-field parity pairs the UI plots. Ranking is by
        mean Field Q² across the chosen fields (unitless, so it aggregates).
        """
        collected: dict[str, Any] = {}
        excluded: dict[str, Any] = {"n_excluded": 0, "reason_counts": {}}
        for spec in field_specs:
            data = await collect_field_training_data(
                session,
                source_kind=source_kind,
                source_id=source_id,
                field_spec=spec,
                factor_filter=factor_filter,
            )
            collected[spec.response_name] = data
            f_excl = {
                "n_excluded": data.n_excluded_invalid,
                "reason_counts": dict(data.excluded_reason_counts),
            }
            excluded = _merge_excluded(excluded, f_excl)

        field_per_method: list[dict[str, Any]] = []
        for method in methods:
            responses: dict[str, Any] = {}
            message: str | None = None
            for spec in field_specs:
                data = collected[spec.response_name]
                if data.n_complete < _MIN_FIELD_RUNS:
                    message = (
                        f"Only {data.n_complete} usable run(s) with a retained "
                        f"'{spec.response_name}' snapshot; need at least {_MIN_FIELD_RUNS}."
                        + _format_exclusions(data.n_excluded_invalid, data.excluded_reason_counts)
                    )
                    continue
                try:
                    # Defaults capture this iteration's data/method; a lambda
                    # whose only parameters are defaulted gives mypy no
                    # signature to infer, hence the ignore.
                    fsurr = await asyncio.to_thread(
                        lambda d=data, m=method: fit_field_surrogate(  # type: ignore[misc]
                            d.snapshots,
                            d.factor_columns,
                            categorical_columns=d.categorical_columns,
                            surrogate_kind=m,
                        )
                    )
                except FieldSurrogateError as exc:
                    message = str(exc)
                    continue
                q = fsurr.quality
                responses[spec.response_name] = {
                    "field_q2": q.field_q2,
                    "recon_rmse": q.recon_rmse,
                    "modes_retained": q.modes_retained,
                    "mode_energy": q.mode_energy,
                    "mode_q2": list(q.mode_q2),
                    "mode_energy_spectrum": list(fsurr.mode_energy_spectrum),
                    "cv_field_actual": list(q.cv_field_actual),
                    "cv_field_predicted": list(q.cv_field_predicted),
                    "n": q.n,
                }
            fqs = [r["field_q2"] for r in responses.values() if r["field_q2"] is not None]
            mean_field_q2 = sum(fqs) / len(fqs) if fqs else None
            field_per_method.append(
                {
                    "method": method,
                    "fitted": bool(responses),
                    "message": None if responses else message,
                    "responses": responses,
                    "mean_field_q2": mean_field_q2,
                }
            )

        rankable = [
            m for m in field_per_method if m["fitted"] and m.get("mean_field_q2") is not None
        ]
        best_field_overall = (
            max(rankable, key=lambda m: m["mean_field_q2"])["method"] if rankable else None
        )
        return field_per_method, best_field_overall, excluded

    async def _fit_all(
        self,
        session: AsyncSession,
        *,
        source_kind: str,
        source_id: int,
        response_names: Sequence[str],
        field_specs: Sequence[FieldSpec],
        factor_filter: FactorFilter | None,
        surrogate_kind: str,
        field_auto_select: bool = False,
        field_surrogate_kind: str | None = None,
        tool_alias: str | None = None,
        tuning_mode: str = "off",
        candidate_methods: Sequence[str] | None = None,
        tuning_overrides: Mapping[str, Mapping[str, str]] | None = None,
        progress: Progress | None = None,
    ) -> _FitResult:
        """Fit the scalar responses + every field spec from one source (ADR-0018).

        Each output trains on its own valid subset (the ingest validity gate,
        ADR-0017). Scalars use ``surrogate_kind``; fields use their own
        ``field_surrogate_kind`` (which may be ``'auto'`` — best regressor per POD
        mode), falling back to the legacy ``field_auto_select`` flag or, failing
        that, ``surrogate_kind``. Shared by create + refit. Raises
        ``ModelConfigError`` if nothing was asked for.
        """
        field_kind = field_surrogate_kind or ("auto" if field_auto_select else surrogate_kind)
        surrogates: dict[str, Surrogate] = {}
        field_surrogates: dict[str, FieldSurrogate] = {}
        field_formats: dict[str, str] = {}
        reference_meshes: dict[str, Path | None] = {}
        field_results: list[tuple[FieldSurrogate, FieldSpec]] = []
        n_points = 0
        constants: dict[str, Any] = {}
        excluded: dict[str, Any] = {"n_excluded": 0, "reason_counts": {}}

        training: Any = None
        decisions: dict[str, TuningDecision] = {}
        if response_names:
            scalar = await self._fit_responses(
                session,
                source_kind,
                source_id,
                response_names,
                factor_filter,
                surrogate_kind,
                tool_alias=tool_alias,
                tuning_mode=tuning_mode,
                candidate_methods=candidate_methods,
                tuning_overrides=tuning_overrides,
                progress=progress,
            )
            surrogates, training, decisions = scalar.surrogates, scalar.training, scalar.decisions
            n_points = max(n_points, scalar.n_points)
            constants.update(scalar.constants)
            excluded = _merge_excluded(excluded, scalar.excluded)

        for fspec in field_specs:
            fsurr, n_f, f_consts, ref, f_excl = await self._fit_field_response(
                session, source_kind, source_id, fspec, factor_filter, field_kind
            )
            field_surrogates[fspec.response_name] = fsurr
            field_formats[fspec.response_name] = fspec.file_format
            reference_meshes[fspec.response_name] = ref
            field_results.append((fsurr, fspec))
            n_points = max(n_points, n_f)
            constants.update(f_consts)
            excluded = _merge_excluded(excluded, f_excl)

        if not surrogates and not field_surrogates:
            raise ModelConfigError("Pick at least one response (scalar or field) to model.")

        factors, responses, quality = _describe_combined(
            surrogates,
            response_names,
            field_results,
            field_auto_select=(field_kind == "auto"),
            field_method=field_kind,
            decisions=decisions,
        )
        constants = _constants_without_factors(constants, factors)
        return _FitResult(
            surrogates=surrogates,
            field_surrogates=field_surrogates,
            field_formats=field_formats,
            reference_meshes=reference_meshes,
            factors=factors,
            responses=responses,
            quality=quality,
            n_points=n_points,
            constants=constants,
            excluded=excluded,
            training=training,
        )

    async def _fit_responses(
        self,
        session: AsyncSession,
        source_kind: str,
        source_id: int,
        response_names: Sequence[str],
        factor_filter: FactorFilter | None,
        surrogate_kind: str,
        *,
        tool_alias: str | None = None,
        tuning_mode: str = "off",
        candidate_methods: Sequence[str] | None = None,
        tuning_overrides: Mapping[str, Mapping[str, str]] | None = None,
        progress: Progress | None = None,
    ) -> _ScalarFits:
        """Fit every scalar result with its ADR-0022 configuration."""
        primary = _validated_methods([surrogate_kind])[0]
        methods = _validated_methods([primary, *(candidate_methods or [])])
        overrides = _validated_overrides(tuning_overrides, response_names, methods)
        minimums = await self._declared_minimums(session, tool_alias) if tool_alias else None
        labels = await self._output_labels(session, tool_alias) if tool_alias else {}
        tuned = await self._tune_responses(
            session,
            source_kind,
            source_id,
            response_names,
            factor_filter,
            methods=methods,
            tuning_mode=tuning_mode,
            minimums=minimums,
            labels=labels,
            progress=progress,
            extra=overrides,
        )
        surrogates: dict[str, Surrogate] = {}
        decisions: dict[str, TuningDecision] = {}
        n_points = 0
        constants: dict[str, Any] = {}
        excluded: dict[str, Any] = {"n_excluded": 0, "reason_counts": {}}
        training: Any = None
        if progress is not None and tuned:
            n_fits = sum(len(t.tuner.planned()) for t in tuned)
            progress(n_fits, n_fits, "Checking which combinations hold up in repeated tests")
        for t in tuned:
            surrogate, decision = await asyncio.to_thread(
                t.tuner.decide,
                primary,  # type: ignore[arg-type]
                overrides.get(t.response),
            )
            if not surrogate.fitted:
                raise ModelConfigError(
                    f"Couldn't fit a surrogate for '{t.response}': "
                    f"{surrogate.quality.message or 'not enough complete runs'}"
                )
            surrogates[t.response] = surrogate
            decisions[t.response] = decision
            # The factor columns are the same across responses (only the
            # response column differs), so the last one describes the design.
            training = t.data
            n_points = max(n_points, t.data.n_complete)
            # Source-level, identical across responses — last wins.
            constants = dict(t.data.input_constants)
            # Validity exclusion is row-level, so identical across responses;
            # keep the largest count seen (ADR-0017 reporting).
            excluded = _larger_exclusion(excluded, t.data)
        return _ScalarFits(surrogates, n_points, constants, excluded, training, decisions)

    async def _tune_responses(
        self,
        session: AsyncSession,
        source_kind: str,
        source_id: int,
        response_names: Sequence[str],
        factor_filter: FactorFilter | None,
        *,
        methods: Sequence[str],
        tuning_mode: str,
        minimums: Mapping[str, float | None] | None,
        labels: Mapping[str, str] | None = None,
        progress: Progress | None = None,
        extra: Mapping[str, Config] | None = None,
    ) -> list[_Tuned]:
        """Collect each result's training data and fit its candidates (ADR-0022).

        Offloads the CPU-bound sklearn fits (GP cross-validation is O(n^3) per
        fold) to a worker thread. The UI and API share one event loop; fitting
        inline blocks NiceGUI's websocket heartbeat for the whole comparison, so
        the browser disconnects mid-fit and the page "breaks and refreshes".
        Tuning is many fits per result, so this matters more than ever.

        Log inputs are offered only where the tool declares a minimum above zero
        (``minimums``); without a readable descriptor nothing is logged, so an
        exported tool can never be handed a 0 it cannot take the log of.
        """
        if tuning_mode not in TUNING_MODES:
            raise ModelConfigError(f"Unknown tuning setting '{tuning_mode}'.")
        tuned: list[_Tuned] = []
        # Live counters the worker-thread progress callback reads: how many fits
        # are done, and the largest candidate count seen so far per result.
        counts = {"done": 0, "per_result": 0}
        for index, response in enumerate(response_names):
            data = await collect_training_data(
                session,
                source_kind=source_kind,
                source_id=source_id,
                response_spec=ResponseSpec(kind="scalar", name=response),
                factor_filter=factor_filter,
            )
            eligible = (
                log_eligible_factors(data.factor_columns, minimums) if minimums is not None else ()
            )
            # The tool's label, never the internal output name (the progress dialog
            # is user-facing).
            shown = (labels or {}).get(response) or response
            label = f"Fitting '{shown}' ({index + 1} of {len(response_names)})"

            def _tick(label: str = label) -> None:
                counts["done"] += 1
                if progress is not None:
                    total = max(counts["done"], counts["per_result"] * len(response_names))
                    progress(counts["done"], total, label)

            tuner = ResultTuner(
                data.factor_columns,
                data.response,
                methods=list(methods),  # type: ignore[arg-type]
                mode=tuning_mode,  # type: ignore[arg-type]
                log_eligible=eligible,
                # A varied categorical is a real axis. Dropped, two runs with
                # identical numbers and different materials became one point
                # with two answers, and the fit carried that as irreducible
                # error — so widening a cohort to a second material made the
                # model *worse* and returned one blended prediction for both.
                categorical_columns=data.categorical_columns,
                source_ref=data.source_ref,
                progress=_tick,
            )
            counts["per_result"] = max(counts["per_result"], len(tuner.planned()))
            override = (extra or {}).get(response)
            await asyncio.to_thread(tuner.evaluate, [override] if override is not None else [])
            tuned.append(_Tuned(response, data, tuner))
        return tuned

    async def _output_labels(self, session: AsyncSession, tool_alias: str) -> dict[str, str]:
        """``{output name: label}`` from the source tool's descriptor (``{}`` if unreadable)."""
        descriptor = await self._latest_descriptor(session, tool_alias)
        if descriptor is None:
            return {}
        return {str(o.name): str(o.label or o.name) for o in descriptor.outputs}

    async def _declared_minimums(
        self, session: AsyncSession, tool_alias: str
    ) -> dict[str, float | None] | None:
        """The source tool's declared minimum per numeric input, or ``None``."""
        descriptor = await self._latest_descriptor(session, tool_alias)
        if descriptor is None:
            return None
        return {
            str(i.name): (float(i.minimum) if i.minimum is not None else None)
            for i in descriptor.inputs
            if i.type in ("integer", "float")
        }

    async def _fit_field_response(
        self,
        session: AsyncSession,
        source_kind: str,
        source_id: int,
        field_spec: FieldSpec,
        factor_filter: FactorFilter | None,
        surrogate_kind: str = "gp",
    ) -> tuple[FieldSurrogate, int, dict[str, Any], Path | None, dict[str, Any]]:
        """Fit a POD field surrogate from the source's retained snapshots (§4.3).

        Returns the fitted surrogate, the training-point count, the held-
        constant inputs, a representative snapshot path (geometry-bearing
        formats) the bundle captures as viewer mesh geometry, and the
        ADR-0017 exclusion summary (``{n_excluded, reason_counts}``).
        """
        data = await collect_field_training_data(
            session,
            source_kind=source_kind,
            source_id=source_id,
            field_spec=field_spec,
            factor_filter=factor_filter,
        )
        if data.n_complete < _MIN_FIELD_RUNS:
            raise ModelConfigError(
                "A field surrogate needs at least two completed runs with retained "
                f"field snapshots; found {data.n_complete}."
                + _format_exclusions(data.n_excluded_invalid, data.excluded_reason_counts)
            )
        try:
            # Offload the CPU-bound POD/SVD + per-mode GP fit (snapshot loading
            # in collect_field_training_data is already threaded). Keeps the
            # shared event loop free so the websocket heartbeat survives a long
            # fit — same reason as the scalar _fit_responses offload.
            field_surrogate = await asyncio.to_thread(
                lambda: fit_field_surrogate(
                    data.snapshots,
                    data.factor_columns,
                    surrogate_kind=surrogate_kind,
                    categorical_columns=data.categorical_columns,
                )
            )
        except FieldSurrogateError as exc:
            raise ModelConfigError(f"Couldn't fit a field surrogate: {exc}") from exc
        reference_mesh: Path | None = None
        if field_spec.file_format.lower().lstrip(".") != "npy" and data.reference_snapshot:
            candidate = Path(data.reference_snapshot)
            if candidate.is_file():
                reference_mesh = candidate
        excluded = {
            "n_excluded": data.n_excluded_invalid,
            "reason_counts": dict(data.excluded_reason_counts),
        }
        return (
            field_surrogate,
            data.n_complete,
            dict(data.input_constants),
            reference_mesh,
            excluded,
        )

    async def _add_version(
        self,
        session: AsyncSession,
        *,
        model: SurrogateModel,
        ordinal: int,
        actor_user_id: int,
        surrogates: dict[str, Surrogate],
        factors: list[dict[str, Any]],
        responses: list[dict[str, Any]],
        quality: dict[str, Any],
        source_kind: str,
        source_id: int,
        tool_version_ids: list[int],
        n_points: int,
        keep_training_data: bool,
        published: bool,
        input_constants: dict[str, Any] | None = None,
        surrogate_method: str = "gp",
        field_surrogates: dict[str, FieldSurrogate] | None = None,
        field_formats: dict[str, str] | None = None,
        reference_meshes: dict[str, Path | None] | None = None,
        training: Any = None,
    ) -> SurrogateModelVersion:
        """Persist a version row then write its bundle.

        ``published`` (personal / project, §6.3) publishes the version
        directly + advances the model's published pointer; a global
        version lands ``draft`` for the two-gate review.

        The version ``kind`` is derived from what was fitted (ADR-0018):
        scalars only → ``scalar``; exactly one field, no scalars → ``field``
        (the single-field POD bundle); anything else with fields (scalars +
        field(s), or several fields) → ``combined`` (one bundle with both
        subtrees).
        """
        field_surrogates = dict(field_surrogates or {})
        field_formats = dict(field_formats or {})
        reference_meshes = dict(reference_meshes or {})
        has_fields = bool(field_surrogates)
        has_scalars = bool(surrogates)
        if has_fields and (has_scalars or len(field_surrogates) > 1):
            kind = "combined"
        elif has_fields:
            kind = "field"
        else:
            kind = "scalar"
        # **Off the event loop** (finding 76). This runs the model-free pass
        # *and* the per-response GP pass — the most expensive coverage path
        # there is, on the largest archive there is — and it ran inline in an
        # `async def`, stalling every other request for the duration. The
        # surrogate fits either side of it were already offloaded; this was
        # not.
        coverage_columns = await asyncio.to_thread(
            _coverage_columns,
            session,
            training,
            tool_alias=model.tool_alias,
            descriptor=await self._latest_descriptor(session, model.tool_alias),
            surrogates=surrogates,
            field_surrogates=field_surrogates,
        )

        version = SurrogateModelVersion(
            surrogate_model_id=model.id,
            ordinal=ordinal,
            kind=kind,
            factors=factors,
            responses=responses,
            source_kind=source_kind,
            source_id=source_id,
            tool_version_ids_at_fit=tool_version_ids,
            n_training_points=n_points,
            bundle_path="(pending)",
            bundle_sha256="(pending)",
            quality=quality,
            keep_training_data=keep_training_data,
            publish_state="published" if published else "draft",
            fitted_by_user_id=actor_user_id,
            input_constants=dict(input_constants or {}),
            surrogate_method=surrogate_method,
            **coverage_columns,
        )
        session.add(version)
        await session.flush()
        bundle_dir = self._bundle_dir(model.id, ordinal)
        # The coverage report is baked into **every** kind of bundle so the
        # exported tool's orchestrator — which runs in its own subprocess with no
        # database — can evaluate the support guard against the same envelope the
        # analysis produced. Passing it only for ``scalar`` meant a combined or
        # field export declared ``support_status`` / ``support_sigma`` in its
        # descriptor (synthesis reads the report from the database) that the
        # orchestrator could never write.
        coverage_report = version.coverage_report_json
        if kind == "combined":
            sha = write_combined_bundle(
                bundle_dir,
                surrogates,
                field_surrogates,
                factors=factors,
                responses=responses,
                field_formats=field_formats,
                reference_meshes=reference_meshes,
                coverage_report=coverage_report,
            )
        elif kind == "field":
            (only,) = field_surrogates  # exactly one field, no scalars
            sha = write_field_bundle(
                bundle_dir,
                field_surrogates[only],
                factors=factors,
                responses=responses,
                file_format=field_formats.get(only, "npy"),
                reference_mesh=reference_meshes.get(only),
                coverage_report=coverage_report,
            )
        else:
            sha = write_scalar_bundle(
                bundle_dir,
                surrogates,
                factors=factors,
                responses=responses,
                coverage_report=coverage_report,
            )
        version.bundle_path = str(bundle_dir)
        version.bundle_sha256 = sha
        if published:
            model.published_version_id = version.id
        await session.flush()
        return version

    async def _latest_descriptor(self, session: AsyncSession, tool_alias: str) -> Any:
        """The tool's latest descriptor, or ``None`` if there isn't a usable one.

        Coverage needs it for two things it cannot get from the training data:
        the **label** every surface should display instead of the internal input
        name ([[feedback_use_labels_not_names]]), and whether a numeric input is
        an **integer** and so a finite lattice rather than a continuum
        (ADR-0020 §3.3).

        Mirrors the cohort service's resolver, including its tolerance: a missing
        or unparseable snapshot degrades the report (names instead of labels, a
        continuous axis instead of a lattice) but must never fail a fit that
        otherwise succeeded.
        """
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        version_id = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias.strip())
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if version_id is None:
            return None
        tool_version = await session.get(ToolVersion, version_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        try:
            return ToolDescriptor.model_validate(snapshot)
        except Exception:  # a stale snapshot degrades the answer, never breaks it
            return None

    def _bundle_dir(self, model_id: int, ordinal: int) -> Path:
        return self.models_root / "models" / self.environment / f"model_{model_id}" / f"v{ordinal}"

    async def _latest_version(
        self, session: AsyncSession, model_id: int
    ) -> SurrogateModelVersion | None:
        return (
            (
                await session.execute(
                    select(SurrogateModelVersion)
                    .where(SurrogateModelVersion.surrogate_model_id == model_id)
                    .order_by(SurrogateModelVersion.ordinal.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def _latest_tool_version_id(self, session: AsyncSession, tool_alias: str) -> int:
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        vid = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias.strip())
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if vid is None:
            raise ModelConfigError(f"'{tool_alias}' has no installed versions.")
        return int(vid)

    async def _can_read(
        self, session: AsyncSession, model: SurrogateModel, actor_user_id: int
    ) -> bool:
        if model.scope == "global":
            return True
        if model.scope == "personal":
            return model.owner_user_id == actor_user_id
        # project
        if model.project_id is None:
            return False
        try:
            await _require_project_access(
                session,
                project_id=model.project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.VIEW,
            )
        except (NotFoundError, AccessDeniedError):
            return False
        return True

    async def _get_with_access(
        self, session: AsyncSession, model_id: int, actor_user_id: int, *, write: bool
    ) -> SurrogateModel:
        model = await session.get(SurrogateModel, model_id)
        if model is None:
            raise NotFoundError("surrogate model", model_id)
        if write:
            await self._require_write(session, model, actor_user_id)
        elif not await self._can_read(session, model, actor_user_id):
            raise NotFoundError("surrogate model", model_id)  # existence-hidden
        return model

    async def _require_write(
        self, session: AsyncSession, model: SurrogateModel, actor_user_id: int
    ) -> None:
        if model.scope == "personal":
            if model.owner_user_id != actor_user_id:
                raise NotFoundError("surrogate model", model.id)
            return
        if model.scope == "project" and model.project_id is not None:
            await _require_project_access(
                session,
                project_id=model.project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.EDIT,
            )
            return
        # Global: the steward (owner) manages it — submit for review, refit,
        # update metadata, retire. The mandatory su/admin *release* (§6.3
        # gate 2) is a separate authority checked in ``release_version``.
        if model.owner_user_id != actor_user_id:
            raise NotFoundError("surrogate model", model.id)

    async def _audit(
        self,
        session: AsyncSession,
        action: str,
        actor_user_id: int,
        model_id: int,
        *,
        payload: dict[str, Any],
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="surrogate_model",
                resource_id=str(model_id),
                outcome="success",
                payload=payload,
            ),
        )


def _changed_fields(
    name: str | None, description: str | None, tags: Sequence[str] | None
) -> set[str]:
    """The metadata field names an update touched (for the audit payload)."""
    fields: set[str] = set()
    if name is not None:
        fields.add("name")
    if description is not None:
        fields.add("description")
    if tags is not None:
        fields.add("tags")
    return fields


def _describe_fit(
    surrogates: dict[str, Surrogate],
    response_names: Sequence[str],
    decisions: Mapping[str, TuningDecision] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Frozen factors + responses + quality summary from the fitted surrogates.

    ``quality`` carries the cross-validated ``cv_actual`` / ``cv_predicted``
    held-out pairs alongside the Q²/RMSE so the model detail page can draw a
    predicted-vs-actual parity plot without retaining the training data.
    """
    first = next(iter(surrogates.values()))
    factors = [{"name": name, "min": lo, "max": hi} for name, (lo, hi) in first.ranges.items()]
    # A varied categorical is a factor too, described by its levels rather than
    # a range — the exported tool renders it as the same picker the source tool
    # had, and the support guard checks membership rather than containment.
    factors += [
        {"name": name, "levels": list(levels)} for name, levels in first.categorical_levels.items()
    ]
    decisions = decisions or {}
    responses = []
    for r in response_names:
        row: dict[str, Any] = {"name": r, "kind": "scalar"}
        decision = decisions.get(r)
        if decision is not None:
            # What a refit needs to reproduce the choice (ADR-0022), and what the
            # exporter needs to declare `_std` outputs per result.
            row |= {
                "method": decision.config.method,
                "output_scale": decision.config.output_scale,
                "input_scale": decision.config.input_scale,
                "log_factors": list(decision.log_factors),
                "tuning_source": decision.source,
                "tuning_mode": decision.mode,
                "tuning_methods": sorted(
                    {c.config.method for c in decision.candidates} | {decision.primary_method}
                ),
            }
        responses.append(row)
    quality = {r: _quality_row(s, decisions.get(r)) for r, s in surrogates.items()}
    return factors, responses, quality


def _quality_row(surrogate: Surrogate, decision: TuningDecision | None) -> dict[str, Any]:
    """One scalar result's quality: CV metrics plus the ADR-0022 tuning evidence."""
    q = surrogate.quality
    row: dict[str, Any] = {
        "q2": q.q2,
        "rmse_cv": q.rmse_cv,
        "n": q.n,
        "cv_actual": [float(v) for v in q.actual],
        "cv_predicted": [float(v) for v in q.predicted],
        "method": surrogate.kind,
        "output_scale": surrogate.output_scale,
        "input_scale": "log" if surrogate.log_factors else "as_measured",
        "log_factors": list(surrogate.log_factors),
    }
    if decision is not None:
        row["tuning"] = decision.to_json()
    return row


def _cv_pairs(surrogate: Surrogate) -> dict[str, list[float]]:
    q = surrogate.quality
    if not surrogate.fitted:
        return {"cv_actual": [], "cv_predicted": []}
    return {
        "cv_actual": [float(v) for v in q.actual],
        "cv_predicted": [float(v) for v in q.predicted],
    }


def _mean_q2(fits: Any) -> float | None:
    q2s = [f.quality.q2 for f in fits if f.quality.q2 is not None]
    return sum(q2s) / len(q2s) if q2s else None


def _tuning_audit(responses: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """``{result: "method|output|inputs (source)"}`` for the audit payload."""
    return {
        str(r["name"]): (
            f"{r['method']}|{r.get('output_scale')}|{r.get('input_scale')} "
            f"({r.get('tuning_source')})"
        )
        for r in responses
        if r.get("method")
    }


@dataclass(frozen=True, slots=True)
class _RecordedTuning:
    mode: str
    methods: list[str]
    overrides: dict[str, dict[str, str]]


def _recorded_tuning(responses: Sequence[Mapping[str, Any]], primary: str) -> _RecordedTuning:
    """How a version was tuned, for a refit to reproduce (ADR-0022 §9).

    Reads ADR-0022 rows directly. An ADR-0021 row (``output_scale_mode``) maps
    ``both``/``log`` to *automatic* and keeps a user's output-scale choice on the
    primary method; a version fitted before either is *off*, so it refits
    exactly as it was fitted.
    """
    rows = [r for r in responses if r.get("kind") != "field"]
    mode = next((str(r["tuning_mode"]) for r in rows if r.get("tuning_mode")), None)
    methods: set[str] = {primary}
    overrides: dict[str, dict[str, str]] = {}
    if mode is not None:
        for r in rows:
            methods.update(str(m) for m in r.get("tuning_methods") or [])
            if r.get("tuning_source") == "user" and r.get("method"):
                overrides[str(r["name"])] = {
                    "method": str(r["method"]),
                    "output_scale": str(r.get("output_scale") or "as_measured"),
                    "input_scale": str(r.get("input_scale") or "as_measured"),
                }
        return _RecordedTuning(mode, sorted(methods), overrides)
    legacy = next((str(r["output_scale_mode"]) for r in rows if r.get("output_scale_mode")), None)
    for r in rows:
        if r.get("output_scale_source") == "user" and r.get("output_scale"):
            overrides[str(r["name"])] = {
                "method": primary,
                "output_scale": str(r["output_scale"]),
                "input_scale": "as_measured",
            }
    mode = "automatic" if legacy in ("both", "log") else "off"
    return _RecordedTuning(mode, sorted(methods), overrides)


def _validated_methods(methods: Sequence[str]) -> list[str]:
    out: list[str] = []
    for m in methods:
        if m not in METHODS:
            raise ModelConfigError(f"Unknown fitting method '{m}'.")
        if m not in out:
            out.append(m)
    if not out:
        raise ModelConfigError("Pick at least one fitting method.")
    return out


def _validated_overrides(
    overrides: Mapping[str, Mapping[str, str]] | None,
    response_names: Sequence[str],
    methods: Sequence[str],
) -> dict[str, Config]:
    out: dict[str, Config] = {}
    for name, choice in (overrides or {}).items():
        if name not in response_names:
            raise ModelConfigError(
                f"A fitting choice was given for a result that isn't being fitted: {name}."
            )
        method = str(choice.get("method") or "")
        output_scale = str(choice.get("output_scale") or "as_measured")
        input_scale = str(choice.get("input_scale") or "as_measured")
        if method not in METHODS:
            raise ModelConfigError(f"Unknown fitting method '{method}' for {name}.")
        if output_scale not in ("as_measured", "log") or input_scale not in ("as_measured", "log"):
            raise ModelConfigError(f"Unknown scale for {name}.")
        if method not in methods:
            raise ModelConfigError(
                f"{name} was set to a method that isn't among the methods tried: {method}."
            )
        out[name] = Config(method, output_scale, input_scale)  # type: ignore[arg-type]
    return out


def _larger_exclusion(current: dict[str, Any], data: Any) -> dict[str, Any]:
    if data.n_excluded_invalid > int(current["n_excluded"]):
        return {
            "n_excluded": data.n_excluded_invalid,
            "reason_counts": dict(data.excluded_reason_counts),
        }
    return current


def _describe_field_fit(
    surrogate: FieldSurrogate,
    field_spec: FieldSpec,
    *,
    auto_select: bool = False,
    field_method: str = "gp",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Frozen factors + a single field response + quality from a POD fit.

    Factors (including the reserved ``time`` factor when time-resolved)
    come from the per-mode coefficient surrogates, which share one factor
    space. The response row carries the field metadata the bundle + viewer
    need (plus ``auto_select`` so a refit reproduces per-mode best-pick);
    ``quality`` records the modal reconstruction summary (§4.3).
    """
    first = surrogate.per_mode_surrogates[0]
    factors = [{"name": name, "min": lo, "max": hi} for name, (lo, hi) in first.ranges.items()]
    factors += [
        {"name": name, "levels": list(levels)} for name, levels in first.categorical_levels.items()
    ]
    response = {
        "name": field_spec.response_name,
        "kind": "field",
        "file_format": field_spec.file_format.lower().lstrip("."),
        "field_node_array_name": field_spec.node_array_name,
        "field_timesteps": field_spec.timesteps_glob,
        "time_resolved": field_spec.time_resolved,
        "auto_select": auto_select,
        "field_method": field_method,
    }
    quality = {
        field_spec.response_name: {
            "modes_retained": surrogate.quality.modes_retained,
            "mode_energy": surrogate.quality.mode_energy,
            "mode_energy_spectrum": list(surrogate.mode_energy_spectrum),
            "recon_rmse": surrogate.quality.recon_rmse,
            "mode_q2": list(surrogate.quality.mode_q2),
            "field_q2": surrogate.quality.field_q2,
            "n_nodes": surrogate.n_nodes,
            "n": surrogate.quality.n,
        }
    }
    return factors, [response], quality


_EXCLUSION_REASON_LABELS = {
    "non_finite": "non-finite value",
    "below_min": "below min",
    "above_max": "above max",
    "field_non_finite": "field had NaN/Inf",
    "field_out_of_bounds": "field outside its min/max",
}


def _format_exclusions(n_excluded: int, reason_counts: dict[str, Any]) -> str:
    """Plain-English tail for a 'too few usable runs' message (ADR-0017).

    Turns ``{'field_out_of_bounds': 247}`` into
    ``" 247 run(s) were excluded (247 field outside its min/max) — relax the
    output's validity bounds or fix the runs."`` so the user knows *why* nothing
    was usable, not just that nothing was.
    """
    if not n_excluded:
        return ""
    parts = [
        f"{count} {_EXCLUSION_REASON_LABELS.get(code, code)}"
        for code, count in reason_counts.items()
    ]
    detail = f" ({', '.join(parts)})" if parts else ""
    return (
        f" {n_excluded} run(s) were excluded for output-validity{detail} — "
        "relax the output's validity bounds (or fix the runs) and retry."
    )


def _merge_excluded(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Keep the larger ADR-0017 exclusion summary (validity is row-level)."""
    return b if int(b.get("n_excluded", 0)) > int(a.get("n_excluded", 0)) else a


def _merge_factors(factor_lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Union per-output factor ranges by name (min of mins, max of maxs).

    A combined model fits each output on its own valid subset (ADR-0017), so
    the ranges can differ slightly; the declared factor envelope covers all of
    them, first-seen order preserved.
    """
    merged: dict[str, dict[str, Any]] = {}
    for factors in factor_lists:
        for f in factors:
            name = str(f.get("name"))
            if f.get("levels") is not None:
                # Categorical: union the levels each output was fitted over.
                seen = merged.setdefault(name, {"name": name, "levels": []})
                for level in f["levels"]:
                    if level not in seen["levels"]:
                        seen["levels"].append(level)
                continue
            lo, hi = f.get("min"), f.get("max")
            if name not in merged:
                merged[name] = {"name": name, "min": lo, "max": hi}
                continue
            cur = merged[name]
            if lo is not None:
                cur["min"] = lo if cur["min"] is None else min(cur["min"], lo)
            if hi is not None:
                cur["max"] = hi if cur["max"] is None else max(cur["max"], hi)
    return list(merged.values())


def _describe_combined(
    surrogates: dict[str, Surrogate],
    response_names: Sequence[str],
    field_results: list[tuple[FieldSurrogate, FieldSpec]],
    *,
    field_auto_select: bool = False,
    field_method: str = "gp",
    decisions: Mapping[str, TuningDecision] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Merge scalar + field describes into one (factors, responses, quality).

    Reuses :func:`_describe_fit` / :func:`_describe_field_fit` per output and
    unions the factor envelopes; response rows + quality keys don't collide
    (distinct output names). ``field_auto_select`` is recorded on each field
    response so a refit reproduces the per-mode best-pick.
    """
    factor_lists: list[list[dict[str, Any]]] = []
    responses: list[dict[str, Any]] = []
    quality: dict[str, Any] = {}
    if surrogates:
        f, r, q = _describe_fit(surrogates, response_names, decisions)
        factor_lists.append(f)
        responses.extend(r)
        quality.update(q)
    for fsurr, fspec in field_results:
        f, r, q = _describe_field_fit(
            fsurr, fspec, auto_select=field_auto_select, field_method=field_method
        )
        factor_lists.append(f)
        responses.extend(r)
        quality.update(q)
    return _merge_factors(factor_lists), responses, quality


def _field_specs_from_version(version: SurrogateModelVersion) -> list[FieldSpec]:
    """Reconstruct every field :class:`FieldSpec` a version was fitted with (refit).

    Works for ``field`` (one) and ``combined`` (zero or more) versions; returns
    ``[]`` for a scalar version, so the refit path stays scalar-only there.
    """
    specs: list[FieldSpec] = []
    for r in list(version.responses or []):
        if r.get("kind") != "field":
            continue
        specs.append(
            FieldSpec(
                response_name=str(r.get("name") or ""),
                file_format=str(r.get("file_format") or "npy"),
                node_array_name=r.get("field_node_array_name"),
                timesteps_glob=r.get("field_timesteps"),
                value_min=r.get("value_min"),
                value_max=r.get("value_max"),
            )
        )
    return specs


def _constants_without_factors(
    constants: dict[str, Any], factors: list[dict[str, Any]]
) -> dict[str, Any]:
    """Drop any varied-factor names from the held-constant map (belt-and-braces)."""
    factor_names = {str(f.get("name")) for f in factors}
    return {k: v for k, v in constants.items() if k not in factor_names}


def make_model_service(audit: AuditWriter, *, models_root: Path, environment: str) -> ModelService:
    """Wire a :class:`ModelService` over the audit writer + bundle root."""
    return ModelService(audit, models_root=models_root, environment=environment)
