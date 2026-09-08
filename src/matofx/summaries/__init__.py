"""Auto-generated results summaries.

A deterministic, air-gap-safe **templated narrative** for every terminal
result — single jobs, workflow runs, parametric studies and optimisation
studies — plus an optional, governed **LLM enrichment** that re-proses the
*same* audited facts behind a defence-in-depth guard stack
(``matofx.llm``). Design: ``designs/auto-summaries.md``.

The templated path (``facts`` → ``narrative``) is always on and never touches
the network. Enrichment is opt-in twice (operator wires ``[llm]`` *and* flips
``llm_summaries_enabled``) with a per-user opt-out, and any failure falls back
silently to the templated baseline.
"""

from __future__ import annotations

from matofx.summaries.collectors import (
    collect_job_facts,
    collect_optimisation_facts,
    collect_parametric_facts,
    collect_workflow_facts,
)
from matofx.summaries.errors import (
    NonTerminalResultError,
    SummaryError,
    UnknownSurfaceKindError,
)
from matofx.summaries.facts import (
    SURFACE_KINDS,
    TERMINAL_STATES,
    HeadlineNumber,
    SummaryFacts,
    SurfaceKind,
    is_terminal,
)
from matofx.summaries.grounding import GuardResult, assemble_prompt, run_guards
from matofx.summaries.narrative import (
    SummaryText,
    render_job,
    render_optimisation,
    render_parametric,
    render_workflow,
)
from matofx.summaries.orm import ResultSummary
from matofx.summaries.service import (
    CachedSummary,
    EnrichmentContext,
    SummaryService,
    enrichment_context_if_enabled,
    make_summary_service,
)

__all__ = [
    "SURFACE_KINDS",
    "TERMINAL_STATES",
    "CachedSummary",
    "EnrichmentContext",
    "GuardResult",
    "HeadlineNumber",
    "NonTerminalResultError",
    "ResultSummary",
    "SummaryError",
    "SummaryFacts",
    "SummaryService",
    "SummaryText",
    "SurfaceKind",
    "UnknownSurfaceKindError",
    "assemble_prompt",
    "collect_job_facts",
    "collect_optimisation_facts",
    "collect_parametric_facts",
    "collect_workflow_facts",
    "enrichment_context_if_enabled",
    "is_terminal",
    "make_summary_service",
    "render_job",
    "render_optimisation",
    "render_parametric",
    "render_workflow",
    "run_guards",
]
