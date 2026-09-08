"""Parametric studies — fan a single tool over a generated input matrix.

A parametric study is the defining SPDM design-space-exploration
capability (ADR-0001). It is a **child of project, parallel to a
scenario**: a :class:`ParametricStudy` row pins one tool ``(alias,
version)`` and a factor specification, generates an input matrix
(explicit list / grid / screening / space-filling / RSM), and fans
the surviving points out as ordinary ``Job`` rows that carry
``parametric_study_id`` (and ``scenario_id = NULL``).

This package is built in slices (design §14, "2-PS-a"):

- **Slice 1 (this module set):** the data model — :class:`ParametricStudy`
  ORM + the closed ``STUDY_STATES`` / ``STUDY_MODES`` vocabularies. The
  ``job`` table gains ``parametric_study_id`` and the home-exactly-one
  CHECK in :mod:`matofx.hierarchy.orm`. Migration ``0032`` ships the
  schema; the ``parametric_studies`` feature flag and the
  ``parametric_studies.create`` permission gate the surface.

Later slices add the sampling-plan generator, the study service +
tool-version pinning, execution wiring, the API routes, the UI, and
the single study-level approval gate.
"""

from __future__ import annotations

from matofx.studies.analysis import (
    AnovaResult,
    AnovaTerm,
    FactorSensitivity,
    ResponseSummary,
    RsmFit,
    SensitivityResult,
    anova_contributions,
    compute_sensitivity,
    fit_rsm,
    main_effects,
    pareto_indices,
    rsm_predict,
    summarize,
    varied_columns,
)
from matofx.studies.errors import (
    FactorSpecError,
    ModeNotApplicableError,
    NoValidPointsError,
    RunCapExceededError,
    StudyGenerationError,
    StudyStateError,
)
from matofx.studies.factors import Factor, FactorSpec, available_modes
from matofx.studies.generate import GenerateResult, SamplingParams, generate
from matofx.studies.orm import (
    SOURCE_KINDS,
    STUDY_MODES,
    STUDY_STATES,
    ParametricStudy,
    ParametricStudyResult,
    SourceKind,
    StudyMode,
    StudyResult,
    StudyState,
)
from matofx.studies.results_service import (
    ResultsService,
    StudyChildResult,
    StudyResultsView,
    make_results_service,
    record_study_result,
)
from matofx.studies.series_service import (
    AggregateView,
    ChildAggregate,
    ChildSeries,
    SeriesService,
    SeriesView,
    make_series_service,
)
from matofx.studies.service import (
    DEFAULT_RUN_CAP,
    StudyService,
    compute_state,
    make_study_service,
)
from matofx.studies.surrogate import (
    FieldPrediction,
    FieldQuality,
    FieldSurrogate,
    FieldSurrogateError,
    SobolIndex,
    SourceRef,
    Surrogate,
    SurrogateKind,
    SurrogateQuality,
    fit_field_surrogate,
    fit_surrogate,
    load_field_snapshot,
    optimize_surrogate,
    sobol_indices,
)
from matofx.studies.surrogate.ingest import (
    FactorFilter,
    IngestError,
    ResponseSpec,
    TrainingData,
    collect_training_data,
)

__all__ = [
    "DEFAULT_RUN_CAP",
    "SOURCE_KINDS",
    "STUDY_MODES",
    "STUDY_STATES",
    "AggregateView",
    "AnovaResult",
    "AnovaTerm",
    "ChildAggregate",
    "ChildSeries",
    "Factor",
    "FactorFilter",
    "FactorSensitivity",
    "FactorSpec",
    "FactorSpecError",
    "FieldPrediction",
    "FieldQuality",
    "FieldSurrogate",
    "FieldSurrogateError",
    "GenerateResult",
    "IngestError",
    "ModeNotApplicableError",
    "NoValidPointsError",
    "ParametricStudy",
    "ParametricStudyResult",
    "ResponseSpec",
    "ResponseSummary",
    "ResultsService",
    "RsmFit",
    "RunCapExceededError",
    "SamplingParams",
    "SensitivityResult",
    "SeriesService",
    "SeriesView",
    "SobolIndex",
    "SourceKind",
    "SourceRef",
    "StudyChildResult",
    "StudyGenerationError",
    "StudyMode",
    "StudyResult",
    "StudyResultsView",
    "StudyService",
    "StudyState",
    "StudyStateError",
    "Surrogate",
    "SurrogateKind",
    "SurrogateQuality",
    "TrainingData",
    "anova_contributions",
    "available_modes",
    "collect_training_data",
    "compute_sensitivity",
    "compute_state",
    "fit_field_surrogate",
    "fit_rsm",
    "fit_surrogate",
    "generate",
    "load_field_snapshot",
    "main_effects",
    "make_results_service",
    "make_series_service",
    "make_study_service",
    "optimize_surrogate",
    "pareto_indices",
    "record_study_result",
    "rsm_predict",
    "sobol_indices",
    "summarize",
    "varied_columns",
]
