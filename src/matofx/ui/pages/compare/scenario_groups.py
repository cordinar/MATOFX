"""Pure matching logic for Compare Scenarios.

Given several scenarios and what each contains — its standalone jobs and
its workflow runs — work out what's comparable *across* them:

- **Per-tool groups** — for every tool that has a standalone job in two
  or more of the selected scenarios, line those jobs up side-by-side,
  one column per scenario (``None`` where a scenario doesn't have that
  tool, which the renderer turns into a top-aligned placeholder).
- **Per-workflow groups** — same idea for workflow runs: a workflow
  definition run in two or more scenarios becomes a workflow comparison,
  one run per scenario.
- **Not comparable** — tools / workflows present in only one of the
  selected scenarios. Listed for the user so nothing silently vanishes,
  but not rendered as a comparison (there's nothing to compare against).

The "at least two scenarios" rule decides what's a *comparison*; within
a rendered group, a scenario that lacks the tool/workflow still gets a
column (a placeholder) so the columns stay aligned — see the locked
2026-05-24 UI rule.

Each scenario holds at most one standalone job per tool (the
``one tool per scenario`` invariant) and at most one run per workflow
definition (``WorkflowRun`` is unique per ``(scenario, workflow)``), so
the per-scenario slot is always unambiguous.

Pure: no NiceGUI, no network — unit-testable in isolation, mirroring
:mod:`compare.selection` and :mod:`compare.workflow_groups`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScenarioToolGroup:
    """A tool compared across scenarios (standalone jobs, one per column).

    ``job_ids`` has one entry per selected scenario, in selection order;
    ``None`` marks a scenario without a standalone job for this tool.
    """

    tool_alias: str
    job_ids: tuple[int | None, ...]


@dataclass(frozen=True)
class ScenarioWorkflowGroup:
    """A workflow definition compared across scenarios (one run per column).

    ``runs`` has one entry per selected scenario, in selection order;
    ``None`` marks a scenario without a run of this workflow. Each entry
    is the run payload (``WorkflowRunResponse`` shape) so the renderer
    can hand it straight to the workflow comparison machinery.
    """

    workflow_alias: str
    workflow_name: str
    runs: tuple[dict[str, Any] | None, ...]


@dataclass(frozen=True)
class NotComparableItem:
    """A tool / workflow present in only one of the selected scenarios.

    ``name`` is a best-effort display name — the workflow name for
    workflows; for tools it's the alias (the renderer resolves the human
    tool name before showing it). ``scenario_label`` names the one
    scenario that has it.
    """

    kind: str
    alias: str
    name: str
    scenario_label: str


@dataclass(frozen=True)
class ScenarioComparison:
    """The full plan for comparing several scenarios side-by-side."""

    scenario_ids: tuple[int, ...]
    scenario_names: tuple[str, ...]
    cross_project: bool
    tool_groups: tuple[ScenarioToolGroup, ...]
    workflow_groups: tuple[ScenarioWorkflowGroup, ...]
    not_comparable: tuple[NotComparableItem, ...]


def match_scenarios(
    scenarios: list[dict[str, Any]],
    standalone_jobs_by_scenario: dict[int, list[dict[str, Any]]],
    runs_by_scenario: dict[int, list[dict[str, Any]]],
) -> ScenarioComparison:
    """Work out the comparable tool / workflow groups across scenarios.

    ``scenarios`` fixes the column order (and supplies names + project
    ids for the cross-project flag). ``standalone_jobs_by_scenario`` maps
    each scenario id to its standalone jobs (workflow-run children must
    already be filtered out by the caller, but a stray child is ignored
    defensively). ``runs_by_scenario`` maps each scenario id to its
    workflow runs.
    """
    scenario_ids = [int(s["id"]) for s in scenarios]
    scenario_names = [str(s.get("name") or s["id"]) for s in scenarios]
    n = len(scenario_ids)
    project_ids = {s.get("project_id") for s in scenarios if s.get("project_id") is not None}
    cross_project = len(project_ids) > 1

    # tool_alias -> per-scenario standalone job id (slot list of length n).
    tool_slots: dict[str, list[int | None]] = {}
    for idx, sid in enumerate(scenario_ids):
        for job in standalone_jobs_by_scenario.get(sid, []):
            if job.get("workflow_run_id") is not None:
                continue
            alias = str(job.get("tool_alias") or "")
            job_id = job.get("id")
            if not alias or job_id is None:
                continue
            tool_slots.setdefault(alias, [None] * n)[idx] = int(job_id)

    # workflow_alias -> per-scenario run payload + a representative name.
    workflow_slots: dict[str, list[dict[str, Any] | None]] = {}
    workflow_names: dict[str, str] = {}
    for idx, sid in enumerate(scenario_ids):
        for run in runs_by_scenario.get(sid, []):
            alias = str(run.get("workflow_alias") or "")
            if not alias:
                continue
            workflow_names.setdefault(alias, str(run.get("workflow_name") or alias))
            workflow_slots.setdefault(alias, [None] * n)[idx] = run

    tool_groups: list[ScenarioToolGroup] = []
    workflow_groups: list[ScenarioWorkflowGroup] = []
    not_comparable: list[NotComparableItem] = []

    for alias in sorted(tool_slots):
        job_slots = tool_slots[alias]
        present = [i for i, value in enumerate(job_slots) if value is not None]
        if len(present) >= 2:
            tool_groups.append(ScenarioToolGroup(tool_alias=alias, job_ids=tuple(job_slots)))
        else:
            not_comparable.append(
                NotComparableItem(
                    kind="tool",
                    alias=alias,
                    name=alias,
                    scenario_label=scenario_names[present[0]],
                )
            )

    for alias in sorted(workflow_slots):
        run_slots = workflow_slots[alias]
        present = [i for i, value in enumerate(run_slots) if value is not None]
        name = workflow_names[alias]
        if len(present) >= 2:
            workflow_groups.append(
                ScenarioWorkflowGroup(
                    workflow_alias=alias,
                    workflow_name=name,
                    runs=tuple(run_slots),
                )
            )
        else:
            not_comparable.append(
                NotComparableItem(
                    kind="workflow",
                    alias=alias,
                    name=name,
                    scenario_label=scenario_names[present[0]],
                )
            )

    return ScenarioComparison(
        scenario_ids=tuple(scenario_ids),
        scenario_names=tuple(scenario_names),
        cross_project=cross_project,
        tool_groups=tuple(tool_groups),
        workflow_groups=tuple(workflow_groups),
        not_comparable=tuple(not_comparable),
    )
