"""Per-surface fact collectors.

Each module composes the surface's *existing* domain services into a
:class:`~matofx.summaries.facts.SummaryFacts` — no new persistence, no new
heavy query. Collectors are best-effort (a missing sub-fact degrades to
"not available" rather than raising) and refuse non-terminal results.
"""

from __future__ import annotations

from matofx.summaries.collectors.job import collect_job_facts
from matofx.summaries.collectors.optimisation import collect_optimisation_facts
from matofx.summaries.collectors.parametric import collect_parametric_facts
from matofx.summaries.collectors.workflow import collect_workflow_facts

__all__ = [
    "collect_job_facts",
    "collect_optimisation_facts",
    "collect_parametric_facts",
    "collect_workflow_facts",
]
