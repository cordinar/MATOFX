"""Optimisation studies — the second study sibling (ADR-0009 §3).

An optimisation study drives an adaptive outer loop (propose → run →
learn → repeat) over one tool's factor space until a stopping criterion
fires, parallel to ``Scenario`` / ``ParametricStudy`` / ``JobCohort``
under a ``Project`` (``designs/studies-engine.md`` §1). It feeds the
unified Results explorer via ``(source_kind='optimisation',
source_id=study.id)`` over the ``study_result`` store, exactly as its
siblings do.

This slice (build Phase 1) ships the data model
(:class:`OptimisationStudy` + migration 0066), the ``configure``-time
:func:`~matofx.studies.optimisation.config.validate_config` gate, and
:class:`OptimisationService` (config + lifecycle CRUD). The strategy
registry, the outer-loop runtime, the run modes, and the UI land in the
following build phases (design §22).
"""

from __future__ import annotations

from matofx.studies.optimisation.config import (
    RUN_MODES,
    STRATEGY_OBJECTIVE_SHAPES,
    validate_config,
)
from matofx.studies.optimisation.errors import (
    OptimisationConfigError,
    OptimisationError,
    OptimisationStateError,
)
from matofx.studies.optimisation.navigator import (
    StrategyRecommendation,
    recommend_strategy,
)
from matofx.studies.optimisation.orm import (
    OPTIMISATION_STUDY_STATES,
    TARGET_KINDS,
    OptimisationStudy,
    OptimisationStudyState,
    TargetKind,
)
from matofx.studies.optimisation.service import (
    OptimisationService,
    make_optimisation_service,
)

__all__ = [
    "OPTIMISATION_STUDY_STATES",
    "RUN_MODES",
    "STRATEGY_OBJECTIVE_SHAPES",
    "TARGET_KINDS",
    "OptimisationConfigError",
    "OptimisationError",
    "OptimisationService",
    "OptimisationStateError",
    "OptimisationStudy",
    "OptimisationStudyState",
    "StrategyRecommendation",
    "TargetKind",
    "make_optimisation_service",
    "recommend_strategy",
    "validate_config",
]
