"""Job cohorts — the third study sibling (ADR-0008 §3).

A job cohort is a **saved query** over completed jobs of one tool,
parallel to ``Scenario`` / ``ParametricStudy`` / ``OptimisationStudy``
under a ``Project`` (``designs/studies-engine.md`` §1). Membership is
computed live (non-negotiable #1) so a new matching run joins
automatically; the cohort feeds the unified surrogate engine + Results
explorer via ``(source_kind='cohort', source_id=cohort.id)`` over the
``study_result`` store.

This slice ships the data model (:class:`JobCohort` + migration 0059,
table renamed to ``job_cohort`` by migration 0073)
and :class:`CohortService` (lifecycle + live membership with
cross-project ACL intersect + tolerated-version divergence warnings).
The lazy ``study_result`` populate, the API router, and the Cohorts UI
tab land in the following slices.
"""

from __future__ import annotations

from matofx.studies.cohort.errors import (
    CohortConfigError,
    CohortError,
    CohortStateError,
)
from matofx.studies.cohort.orm import COHORT_STATES, CohortState, JobCohort
from matofx.studies.cohort.service import (
    CandidateRow,
    CohortService,
    NumericInput,
    OutputColumn,
    PreviewResult,
    ResolvedMembers,
    ToolBuildMeta,
    VersionBand,
    make_cohort_service,
)

__all__ = [
    "COHORT_STATES",
    "CandidateRow",
    "CohortConfigError",
    "CohortError",
    "CohortService",
    "CohortState",
    "CohortStateError",
    "JobCohort",
    "NumericInput",
    "OutputColumn",
    "PreviewResult",
    "ResolvedMembers",
    "ToolBuildMeta",
    "VersionBand",
    "make_cohort_service",
]
