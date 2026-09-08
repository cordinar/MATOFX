"""Source-agnostic training-data ingestion for the surrogate engine (ADR-0008 §4.1).

A thin layer between the three study siblings (parametric / optimisation
/ cohort) and the existing :func:`matofx.studies.surrogate.fit_surrogate`.
It pulls ``(X, y)`` from the unified ``study_result`` store for one source
and returns it in the exact shape ``fit_surrogate`` already expects, so:

- **parametric callers are unchanged** — the Results explorer keeps
  assembling its columns + response and calling ``fit_surrogate``
  directly (non-negotiable #10);
- **cohort / optimisation callers** get the same surrogate engine for
  free by going through :func:`collect_training_data`.

The factor columns come from each member ``Job.inputs`` (the
``study_result`` store holds outputs, not inputs); the response comes
from the row's ``scalars`` (a named scalar) or a retained ``csv_plot``
file reduced to a scalar (the same ``csv_plot`` column+aggregate
machinery the parametric Results explorer exposes). Field responses are
ADR-0008 Phase 4 — rejected here with a clear message.

``FactorFilter`` reuses the cohort's ``input_ranges`` +
``cross_field_predicate`` shape (§4.1), so a fit can train on a *subset*
of a source without mutating it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from matofx.hierarchy.orm import Job
from matofx.studies.orm import StudyResult
from matofx.studies.surrogate import SourceRef

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from sqlalchemy.ext.asyncio import AsyncSession

ResponseKind = Literal["scalar", "csv_column", "field"]
"""The supported response sources (§4.1). ``field`` goes through the
dedicated :func:`collect_field_training_data` (POD/PCA), not the scalar
``collect_training_data`` path, since its response is a whole nodal field."""

_CSV_AGGREGATES: frozenset[str] = frozenset({"last", "max", "min", "mean"})

TIME_FACTOR = "time"
"""Reserved factor name a time-resolved field surrogate fits over (ADR-0008,
2026-06-07). Each ``(run, timestep)`` snapshot contributes one training row;
``time`` is the 0-based index of the snapshot within the run's sorted series,
so ``predict({..., 'time': t})`` reconstructs the field at any step."""


class IngestError(Exception):
    """A training-data ingestion request couldn't be satisfied (plain-English)."""


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """Which response to pull from each member run (§4.1).

    - ``kind='scalar'`` — ``name`` is a scalar output in the row's
      ``scalars``.
    - ``kind='csv_column'`` — ``name`` is the retained ``csv_plot``
      output; ``column`` (0-indexed, [[feedback_csv_columns_zero_indexed]])
      is reduced by ``aggregate`` (``last`` / ``max`` / ``min`` / ``mean``).
    - ``kind='field'`` — POD/PCA field surrogate (Phase 4; rejected here).
    """

    kind: ResponseKind
    name: str = ""
    column: int = 0
    aggregate: str = "last"
    at_x: float | None = None


@dataclass(frozen=True, slots=True)
class FactorFilter:
    """Subset the source without mutating it (§4.1) — the cohort filter shape."""

    input_ranges: dict[str, Any] | None = None
    cross_field_predicate: str | None = None
    output_ranges: dict[str, Any] | None = None
    """On-the-fly ``{output: {min, max}}`` filter on each run's scalar *outputs*
    (not persisted as validity). Lets a fit exclude outlier runs that skew it —
    e.g. only runs where a peak stress stays in a physical band — without
    touching the tool's descriptor bounds. A run whose named output is missing or
    outside the band is dropped from the fit (silently, like an input filter)."""


@dataclass(frozen=True, slots=True)
class TrainingData:
    """Aligned ``(X, y)`` for ``fit_surrogate`` + the partial-data banner.

    ``factor_columns`` / ``response`` plug straight into
    ``fit_surrogate(factor_columns, response, source_ref=source_ref)``.
    ``n_rows`` is every member considered (post-filter); ``n_complete``
    is how many carry a finite response — the gap drives the same
    partial-data banner the Results explorer shows.
    """

    factor_columns: dict[str, list[float | None]] = field(default_factory=dict)
    response: list[float | None] = field(default_factory=list)
    n_rows: int = 0
    n_complete: int = 0
    source_ref: SourceRef | None = None

    categorical_columns: dict[str, list[Any]] = field(default_factory=dict)
    """Varied **non-numeric** inputs, row-aligned with ``factor_columns``
    (ADR-0020 §1.1.1).

    ``factor_columns`` keeps numeric inputs only, so a varied ``select`` /
    ``bool`` / ``file`` input would otherwise never reach a caller at all.
    ``fit_surrogate`` ignores this (it fits on the numeric columns, as
    before); design-space coverage needs it because categorical imbalance —
    40 CFRP runs and 2 GFRP — is invisible on every continuous axis and is
    the *silent* one of coverage's three failure modes.

    An input held constant across the training set is **not** here: it is a
    context value and lives in :attr:`input_constants`.
    """

    input_constants: dict[str, Any] = field(default_factory=dict)
    """Inputs held identical across every training run (ADR-0008 §7.6).

    A key present in every kept row with one shared value — the *context*
    the surrogate was fitted in. ``fit_surrogate`` drops these from the
    model (they didn't vary); the exporter surfaces them as the
    "held constant" half of the synthesised tool's input form.
    """

    n_excluded_invalid: int = 0
    """Runs dropped because their result was flagged invalid (ADR-0017)."""

    n_excluded_filtered: int = 0
    """Runs dropped by the caller's :class:`FactorFilter` (ADR-0020 §1.1.1).

    Counted separately from :attr:`n_excluded_invalid` on purpose: an invalid
    result is a solver problem, a filtered one is the user's own on-the-fly
    exclusion, and the remedies differ. Without this the "fitted on 62 of 100"
    note cannot say where the other 38 went.
    """

    excluded_reason_counts: dict[str, int] = field(default_factory=dict)
    """Per-reason-code counts across the excluded runs, e.g.
    ``{'above_max': 67, 'non_finite': 7}`` — drives the "K excluded (…)" note."""


def _as_number(value: Any) -> float | None:
    """``float(value)`` for a real numeric (not bool / string), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _reason_codes(reasons: Any) -> set[str]:
    """Distinct validity reason codes on a ``study_result`` row (ADR-0017)."""
    out: set[str] = set()
    if isinstance(reasons, list):
        for r in reasons:
            if isinstance(r, dict) and r.get("code"):
                out.add(str(r["code"]))
    return out


def _in_ranges(inputs: dict[str, Any], input_ranges: dict[str, Any] | None) -> bool:
    """Every ``{factor: {min, max}}`` bound holds for this run's inputs."""
    for factor, bound in (input_ranges or {}).items():
        if not isinstance(bound, dict):
            continue
        value = _as_number(inputs.get(factor))
        if value is None:
            return False
        lo, hi = bound.get("min"), bound.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return False
    return True


def _passes_filter(inputs: dict[str, Any], factor_filter: FactorFilter | None) -> bool:
    """Apply ``input_ranges`` + ``cross_field_predicate`` to one run's inputs."""
    if factor_filter is None:
        return True
    if not _in_ranges(inputs, factor_filter.input_ranges):
        return False
    predicate = factor_filter.cross_field_predicate
    if not predicate:
        return True
    from matofx.tools.forms import safe_expr  # noqa: PLC0415

    try:
        return bool(safe_expr.evaluate_condition(predicate, inputs))
    except safe_expr.UnsafeExpressionError:
        return False


def _passes_output_filter(result: Any, factor_filter: FactorFilter | None) -> bool:
    """Apply the on-the-fly ``output_ranges`` to a run's scalar *outputs*.

    A user-chosen subset (not a validity verdict), so a failing run is dropped
    silently — the same treatment an input-range filter gets.
    """
    if factor_filter is None or not factor_filter.output_ranges:
        return True
    return _in_ranges(dict(result.scalars or {}), factor_filter.output_ranges)


async def collect_training_data(
    session: AsyncSession,
    *,
    source_kind: str,
    source_id: int,
    response_spec: ResponseSpec,
    factor_filter: FactorFilter | None = None,
) -> TrainingData:
    """Pull aligned ``(X, y)`` from ``study_result`` rows for one source (§4.1).

    Joins each ``study_result`` row to its member ``Job`` for the input
    vector, extracts the response per ``response_spec``, applies the
    optional ``factor_filter``, and returns columns + response in
    ``fit_surrogate`` shape plus a :class:`SourceRef` tag. Numeric inputs
    only become factor columns; constant / string inputs are dropped by
    the downstream ``fit_surrogate`` (``varied_columns``).
    """
    if response_spec.kind == "field":
        raise IngestError(
            "A field response is fitted through the dedicated field collector "
            "(collect_field_training_data), not the scalar path — pick a scalar or "
            "CSV-column response here."
        )
    if response_spec.kind == "csv_column" and response_spec.aggregate not in _CSV_AGGREGATES:
        raise IngestError(
            f"'{response_spec.aggregate}' isn't a supported CSV aggregate "
            f"({', '.join(sorted(_CSV_AGGREGATES))})."
        )

    rows = (
        await session.execute(
            select(StudyResult, Job)
            .join(Job, Job.id == StudyResult.job_id)
            .where(
                StudyResult.source_kind == source_kind,
                StudyResult.source_id == source_id,
            )
            .order_by(StudyResult.job_id)
        )
    ).all()

    kept: list[tuple[dict[str, Any], float | None]] = []
    n_excluded_invalid = 0
    n_excluded_filtered = 0
    excluded_reason_counts: dict[str, int] = {}
    for result, job in rows:
        inputs = dict(job.inputs or {})
        # The user's own on-the-fly exclusions. Counted (ADR-0020 §1.1.1) so
        # "fitted on N of M" can say which gate took which runs — silently
        # dropping them left coverage unable to explain its own point count.
        if not _passes_filter(inputs, factor_filter):
            n_excluded_filtered += 1
            continue
        if not _passes_output_filter(result, factor_filter):
            n_excluded_filtered += 1
            continue
        # Output-validity gate (ADR-0017): never train on a run flagged
        # invalid (non-finite / out-of-bounds outputs). Count it for the
        # "fitted on N of M; K excluded" note rather than dropping silently.
        if (result.validity or "valid") != "valid":
            n_excluded_invalid += 1
            for code in _reason_codes(result.validity_reasons):
                excluded_reason_counts[code] = excluded_reason_counts.get(code, 0) + 1
            continue
        response = await _extract_response(session, result, job, response_spec)
        kept.append((inputs, response))

    # Numeric factor keys, first-seen order preserved.
    factor_keys: list[str] = []
    for inputs, _ in kept:
        for key, value in inputs.items():
            if key not in factor_keys and _as_number(value) is not None:
                factor_keys.append(key)

    factor_columns: dict[str, list[float | None]] = {
        key: [_as_number(inputs.get(key)) for inputs, _ in kept] for key in factor_keys
    }
    constants = _constant_inputs(kept)
    responses: list[float | None] = [r for _, r in kept]
    n_complete = sum(1 for r in responses if r is not None and math.isfinite(r))
    return TrainingData(
        factor_columns=factor_columns,
        categorical_columns=_categorical_columns(kept, factor_keys, constants),
        response=responses,
        n_rows=len(kept),
        n_complete=n_complete,
        source_ref=SourceRef(source_kind=source_kind, source_id=source_id, n_points=n_complete),
        input_constants=constants,
        n_excluded_invalid=n_excluded_invalid,
        n_excluded_filtered=n_excluded_filtered,
        excluded_reason_counts=excluded_reason_counts,
    )


def _categorical_keys(
    inputs_rows: list[dict[str, Any]],
    factor_keys: list[str],
    constants: dict[str, Any],
    *,
    extra_skip: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Varied non-numeric input names, first-seen order (ADR-0020 §1.1.1).

    Everything that is neither a numeric factor column nor a held-constant
    context value: the ``select`` / ``bool`` / ``file`` inputs a fit encodes
    but ``factor_columns`` (numeric-only) cannot carry.
    """
    skip = set(factor_keys) | set(constants) | set(extra_skip)
    keys: list[str] = []
    for inputs in inputs_rows:
        for key in inputs:
            if key not in skip and key not in keys:
                keys.append(key)
    return keys


def _categorical_columns(
    kept: list[tuple[dict[str, Any], float | None]],
    factor_keys: list[str],
    constants: dict[str, Any],
) -> dict[str, list[Any]]:
    """Varied non-numeric inputs, row-aligned with the numeric columns."""
    inputs_rows = [inputs for inputs, _ in kept]
    return {
        key: [inputs.get(key) for inputs in inputs_rows]
        for key in _categorical_keys(inputs_rows, factor_keys, constants)
    }


def _constant_inputs(kept: list[tuple[dict[str, Any], float | None]]) -> dict[str, Any]:
    """Inputs present in every kept row with one shared value (held constant)."""
    if not kept:
        return {}
    shared_keys = set(kept[0][0])
    for inputs, _ in kept[1:]:
        shared_keys &= set(inputs)
    constants: dict[str, Any] = {}
    for key in shared_keys:
        values = [inputs.get(key) for inputs, _ in kept]
        if all(v == values[0] for v in values):
            constants[key] = values[0]
    return constants


async def _extract_response(
    session: AsyncSession,
    result: StudyResult,
    job: Job,
    spec: ResponseSpec,
) -> float | None:
    """The scalar response for one member run, per ``spec`` (``None`` on a miss)."""
    if spec.kind == "scalar":
        return _as_number((result.scalars or {}).get(spec.name))
    # csv_column — reduce a retained csv_plot column to a scalar on disk.
    import asyncio  # noqa: PLC0415

    from matofx.studies.series_service import (  # noqa: PLC0415
        _aggregate,
        _csv_filename_for,
        _job_outputs_dir,
        _resolve_under_outputs,
    )

    fname = _csv_filename_for(result.retained_files, spec.name)
    if fname is None:
        return None
    outputs_dir = await _job_outputs_dir(session, job)
    if outputs_dir is None:
        return None
    path = _resolve_under_outputs(outputs_dir, fname)
    if path is None:
        return None
    return await asyncio.to_thread(_aggregate, path, spec.column, spec.aggregate)


# --- Field responses (ADR-0008 §4.1/§4.3 — POD field surrogates) ----------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Which field output to read from each member run, and how (§4.3).

    Sourced from the tool descriptor's ``file_type == "field"`` output:
    ``response_name`` matches the output name; ``file_format`` /
    ``node_array_name`` drive :func:`load_field_snapshot`; a non-empty
    ``timesteps_glob`` marks the field **time-resolved** — every file the
    glob matches in a run's working dir is one ``(run, timestep)`` snapshot.
    """

    response_name: str
    file_format: str
    node_array_name: str | None = None
    timesteps_glob: str | None = None
    value_min: float | None = None
    """ADR-0017: inclusive per-node lower bound. A snapshot with any node below
    this (or non-finite) is dropped at ingest. ``None`` = no lower bound."""
    value_max: float | None = None
    """ADR-0017: inclusive per-node upper bound. ``None`` = no upper bound."""

    @property
    def time_resolved(self) -> bool:
        """Whether this field carries a transient series (``time`` factor)."""
        return bool(self.timesteps_glob)


@dataclass(frozen=True, slots=True)
class FieldTrainingData:
    """Aligned ``(X, snapshots)`` for :func:`fit_field_surrogate`.

    ``factor_columns`` (numeric inputs, plus a :data:`TIME_FACTOR` column
    when time-resolved) is row-aligned with ``snapshots`` — one row per
    ``(run, timestep)``. Runs missing their field file are dropped whole
    (you can't POD a missing row), so ``n_complete`` counts kept *runs*
    and ``n_rows`` counts runs considered post-filter. ``reference_snapshot``
    is one representative file the registry uses to capture viewer geometry.
    """

    factor_columns: dict[str, list[float | None]] = field(default_factory=dict)
    snapshots: list[np.ndarray] = field(default_factory=list)
    n_rows: int = 0
    n_complete: int = 0
    source_ref: SourceRef | None = None

    categorical_columns: dict[str, list[Any]] = field(default_factory=dict)
    """Varied non-numeric inputs, row-aligned per ``(run, timestep)``
    (ADR-0020 §1.1.1) — the field sibling of
    :attr:`TrainingData.categorical_columns`, expanded across snapshots the
    same way the numeric columns are."""

    input_constants: dict[str, Any] = field(default_factory=dict)
    reference_snapshot: str | None = None
    time_resolved: bool = False
    n_nodes: int | None = None

    n_excluded_invalid: int = 0
    """Runs dropped for output-validity reasons (ADR-0017): a result flagged
    invalid (e.g. via its companion scalar) OR a snapshot with a non-finite /
    out-of-[value_min, value_max] node at ingest."""

    n_excluded_filtered: int = 0
    """Runs dropped by the caller's :class:`FactorFilter` (ADR-0020 §1.1.1)."""

    excluded_reason_counts: dict[str, int] = field(default_factory=dict)
    """Per-reason-code counts across excluded runs, e.g.
    ``{'above_max': 67, 'field_out_of_bounds': 4}``."""


async def collect_field_training_data(
    session: AsyncSession,
    *,
    source_kind: str,
    source_id: int,
    field_spec: FieldSpec,
    factor_filter: FactorFilter | None = None,
) -> FieldTrainingData:
    """Pull aligned ``(X, field-snapshots)`` for one source (§4.1/§4.3).

    Joins each ``study_result`` row to its member ``Job``, locates the
    retained field snapshot(s) on disk, loads them with
    :func:`load_field_snapshot`, and returns factor columns + snapshots in
    :func:`fit_field_surrogate` shape. Time-resolved fields contribute one
    row per ``(run, timestep)`` with a :data:`TIME_FACTOR` column. Raises
    :class:`IngestError` if a varying input already uses the reserved
    ``time`` name and the field is time-resolved.
    """
    rows = (
        await session.execute(
            select(StudyResult, Job)
            .join(Job, Job.id == StudyResult.job_id)
            .where(
                StudyResult.source_kind == source_kind,
                StudyResult.source_id == source_id,
            )
            .order_by(StudyResult.job_id)
        )
    ).all()

    # First pass: keep runs that pass the filter, are not flagged invalid, and
    # have resolvable snapshots.
    kept: list[tuple[dict[str, Any], list[Path]]] = []
    n_considered = 0
    n_excluded_invalid = 0
    n_excluded_filtered = 0
    excluded_reason_counts: dict[str, int] = {}
    for result, job in rows:
        inputs = dict(job.inputs or {})
        if not _passes_filter(inputs, factor_filter):
            n_excluded_filtered += 1
            continue
        if not _passes_output_filter(result, factor_filter):
            n_excluded_filtered += 1
            continue
        n_considered += 1
        # Output-validity gate (ADR-0017): skip runs flagged invalid (e.g. via
        # the field's companion scalar) before touching disk. Per-node field
        # bounds are the second layer, applied at load in _assemble_field_rows.
        if (result.validity or "valid") != "valid":
            n_excluded_invalid += 1
            for code in _reason_codes(result.validity_reasons):
                excluded_reason_counts[code] = excluded_reason_counts.get(code, 0) + 1
            continue
        paths = await _resolve_field_snapshots(session, result, job, field_spec)
        if paths:
            kept.append((inputs, paths))

    # Numeric factor keys, first-seen order preserved (mirrors the scalar path).
    factor_keys: list[str] = []
    for inputs, _ in kept:
        for key, value in inputs.items():
            if key not in factor_keys and _as_number(value) is not None:
                factor_keys.append(key)
    if field_spec.time_resolved and TIME_FACTOR in factor_keys:
        raise IngestError(
            f"This tool has an input named {TIME_FACTOR!r}, which clashes with the "
            "reserved factor a time-resolved field surrogate fits over. Rename the "
            "input, or fit this field without the transient series."
        )

    constants = _constant_inputs([(inputs, None) for inputs, _ in kept])
    categorical_keys = _categorical_keys(
        [inputs for inputs, _ in kept], factor_keys, constants, extra_skip={TIME_FACTOR}
    )

    (
        factor_columns,
        categorical_columns,
        snapshots,
        reference_snapshot,
        n_nodes,
        n_dropped_bounds,
        bounds_reason_counts,
    ) = await _assemble_field_rows(kept, factor_keys, categorical_keys, field_spec)
    n_excluded_invalid += n_dropped_bounds
    for code, count in bounds_reason_counts.items():
        excluded_reason_counts[code] = excluded_reason_counts.get(code, 0) + count
    n_used = len(kept) - n_dropped_bounds
    return FieldTrainingData(
        factor_columns=factor_columns,
        categorical_columns=categorical_columns,
        snapshots=snapshots,
        n_rows=n_considered,
        n_complete=n_used,
        source_ref=SourceRef(source_kind=source_kind, source_id=source_id, n_points=n_used),
        input_constants=constants,
        reference_snapshot=reference_snapshot,
        time_resolved=field_spec.time_resolved,
        n_nodes=n_nodes,
        n_excluded_invalid=n_excluded_invalid,
        n_excluded_filtered=n_excluded_filtered,
        excluded_reason_counts=excluded_reason_counts,
    )


async def _assemble_field_rows(
    kept: list[tuple[dict[str, Any], list[Path]]],
    factor_keys: list[str],
    categorical_keys: list[str],
    field_spec: FieldSpec,
) -> tuple[
    dict[str, list[float | None]],
    dict[str, list[Any]],
    list[np.ndarray],
    str | None,
    int | None,
    int,
    dict[str, int],
]:
    """Load each run's snapshot(s) into aligned ``(factor_columns, snapshots)``.

    One row per ``(run, timestep)``; a time-resolved field adds the
    0-based snapshot index as the :data:`TIME_FACTOR` column. Returns a
    representative snapshot path (for viewer geometry), the node count, the
    number of runs **dropped whole** for failing the ADR-0017 per-node check
    (any node non-finite, or outside ``value_min``/``value_max``), and the
    per-reason-code breakdown of those drops.
    """
    import asyncio  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    from matofx.studies.surrogate.field import load_field_snapshot  # noqa: PLC0415

    vmin, vmax = field_spec.value_min, field_spec.value_max
    factor_columns: dict[str, list[float | None]] = {key: [] for key in factor_keys}
    categorical_columns: dict[str, list[Any]] = {key: [] for key in categorical_keys}
    if field_spec.time_resolved:
        factor_columns[TIME_FACTOR] = []
    snapshots: list[np.ndarray] = []
    reference_snapshot: str | None = None
    n_nodes: int | None = None
    n_dropped_bounds = 0
    reason_counts: dict[str, int] = {}

    for inputs, paths in kept:
        # Load all of a run's snapshots first; drop the whole run if any
        # snapshot is non-finite or breaches the per-node value bounds (a POD
        # ensemble must be row-aligned, so a partial run is unusable).
        loaded: list[tuple[int, np.ndarray]] = []
        bad_code: str | None = None
        for t_index, path in enumerate(paths):
            arr = await asyncio.to_thread(
                load_field_snapshot,
                path,
                file_format=field_spec.file_format,
                field_node_array_name=field_spec.node_array_name,
            )
            if not bool(np.isfinite(arr).all()):
                bad_code = "field_non_finite"
            elif (vmin is not None and float(arr.min()) < vmin) or (
                vmax is not None and float(arr.max()) > vmax
            ):
                bad_code = "field_out_of_bounds"
            if bad_code is not None:
                break
            loaded.append((t_index, arr))
        if bad_code is not None:
            n_dropped_bounds += 1
            reason_counts[bad_code] = reason_counts.get(bad_code, 0) + 1
            continue
        for t_index, arr in loaded:
            snapshots.append(arr)
            if reference_snapshot is None:
                reference_snapshot = str(paths[t_index])
                n_nodes = int(arr.shape[0])
            for key in factor_keys:
                factor_columns[key].append(_as_number(inputs.get(key)))
            for key in categorical_keys:
                categorical_columns[key].append(inputs.get(key))
            if field_spec.time_resolved:
                factor_columns[TIME_FACTOR].append(float(t_index))
    return (
        factor_columns,
        categorical_columns,
        snapshots,
        reference_snapshot,
        n_nodes,
        n_dropped_bounds,
        reason_counts,
    )


def _retained_field_filename(retained_files: Any, response_name: str) -> str | None:
    """The retained ``kind == "field"`` file matching ``response_name`` (or None)."""
    if not isinstance(retained_files, list):
        return None
    for entry in retained_files:
        if (
            isinstance(entry, dict)
            and entry.get("kind") == "field"
            and str(entry.get("name") or "") == response_name
            and isinstance(entry.get("filename"), str)
            and entry.get("filename")
        ):
            return str(entry["filename"])
    return None


async def _resolve_field_snapshots(
    session: AsyncSession,
    result: StudyResult,
    job: Job,
    spec: FieldSpec,
) -> list[Path]:
    """On-disk snapshot path(s) for one run (empty when nothing resolves).

    A non-transient field resolves to a single representative file; a
    time-resolved field expands its ``timesteps_glob`` against the run's
    working dir, sorted so ``time`` indexing is stable across runs.
    """
    from matofx.studies.series_service import (  # noqa: PLC0415
        _job_outputs_dir,
        _resolve_under_outputs,
    )

    fname = _retained_field_filename(result.retained_files, spec.response_name)
    if fname is None:
        return []
    outputs_dir = await _job_outputs_dir(session, job)
    if outputs_dir is None:
        return []
    if not spec.time_resolved:
        path = _resolve_under_outputs(outputs_dir, fname)
        return [path] if path is not None else []
    # Transient series: glob is working-dir-relative (e.g. ``outputs/t_*.npy``).
    glob = spec.timesteps_glob
    if glob is None:  # unreachable (time_resolved == bool(timesteps_glob)) — narrows the type
        return []
    working_dir = outputs_dir.parent
    matches: list[Path] = []
    for match in sorted(working_dir.glob(glob)):
        if not match.is_file():
            continue
        try:
            match.resolve().relative_to(working_dir.resolve())
        except (ValueError, OSError):  # escaped the working dir — skip defensively
            continue
        matches.append(match)
    return matches
