"""Design-space coverage — is this archive fit to learn from? (ADR-0020).

A surrogate can be fitted from any Job Cohort. Cohorts built from a
parametric study are space-filling by construction; cohorts built from ad-hoc
single jobs are clustered, often confounded, and sometimes categorically
unbalanced — engineers run what the design review asked for.

Every existing quality signal is **post-fit**, and Q² actively conceals this:
computed by leave-out cross-validation on the points that exist, a tight
cluster scores well precisely because every held-out point has a near
neighbour. High Q², no coverage, false confidence — and the user is then
invited to publish, export-as-tool, and optimise against that model.

**Coverage is a property of the design, not of the fit.** It is measurable
before any response is looked at, it travels with the model as versioned
evidence, and it is enforced again at prediction time. Three diseases,
reported separately: sparsity, confounding, categorical imbalance.

This package is **slice 1** — the response-agnostic, model-free path
(`designs/design-space-coverage.md` §13). It deliberately contains no GP, so
the path cohort preview and every cold-start case depend on stands alone and
is testable without a fitted model.

Nothing here reads global state or touches the DB: callers pass a
``TrainingData``, and every threshold arrives as a
:class:`matofx.config.CoverageSettings`.
"""

from __future__ import annotations

from matofx.studies.coverage.diagnose import CoverageDiagnostics, GroupingMode, diagnose
from matofx.studies.coverage.envelope import (
    Envelope,
    FactorEnvelope,
    FactorKind,
    FactorOverride,
    Scale,
    build_envelope,
    snap_to_step,
)
from matofx.studies.coverage.errors import CoverageError
from matofx.studies.coverage.gp import (
    DEFAULT_PLAN_SIZES,
    ResponseKind,
    ResponseModel,
    SuggestionBatch,
    SuggestionOption,
    SuggestionPlan,
    VarianceField,
    default_plan_sizes,
    relative_sigma,
    suggest_design_points,
    suggest_points,
    suggestion_plan,
    variance_field,
)
from matofx.studies.coverage.guard import Support, SupportStatus, evaluate_support
from matofx.studies.coverage.pairs import PairCoverage, analyse_pairs, one_at_a_time
from matofx.studies.coverage.points import PointSet, resolve_points
from matofx.studies.coverage.report import (
    CoverageReport,
    CoverageWarning,
    Criterion,
    DesignCoverage,
    FactorCoverage,
    Measure,
    Verdict,
    analyse_design,
    build_report,
    edge_threshold_from,
    score_for,
    verdict_for,
)
from matofx.studies.coverage.responses import ResponseCoverage, analyse_responses

__all__ = [
    "DEFAULT_PLAN_SIZES",
    "CoverageDiagnostics",
    "CoverageError",
    "CoverageReport",
    "CoverageWarning",
    "Criterion",
    "DesignCoverage",
    "Envelope",
    "FactorCoverage",
    "FactorEnvelope",
    "FactorKind",
    "FactorOverride",
    "GroupingMode",
    "Measure",
    "PairCoverage",
    "PointSet",
    "ResponseCoverage",
    "ResponseKind",
    "ResponseModel",
    "Scale",
    "SuggestionBatch",
    "SuggestionOption",
    "SuggestionPlan",
    "Support",
    "SupportStatus",
    "VarianceField",
    "Verdict",
    "analyse_design",
    "analyse_pairs",
    "analyse_responses",
    "build_envelope",
    "build_report",
    "default_plan_sizes",
    "diagnose",
    "edge_threshold_from",
    "evaluate_support",
    "one_at_a_time",
    "relative_sigma",
    "resolve_points",
    "score_for",
    "snap_to_step",
    "suggest_design_points",
    "suggest_points",
    "suggestion_plan",
    "variance_field",
    "verdict_for",
]
