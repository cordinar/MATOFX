"""Pure step-alignment logic for Compare Workflows.

Given several workflow-run payloads — the ``WorkflowRunResponse`` shape
(``workflow_alias`` / ``workflow_name`` / ``workflow_version_snapshot`` /
``children`` where each child is ``{id, workflow_position, tool_alias,
tool_name, state, …}``) — produce the per-step alignment the Compare
grid needs: one comparison group per workflow step (member tool), with
each run's child job for that tool slotted into that run's column, or
``None`` when a run doesn't include the step.

A run can be missing a step when the compared runs were started against
different versions of the same workflow definition (member sets drift
between versions). The renderer turns a ``None`` slot into a top-aligned
"this step isn't part of this run" placeholder so the columns stay
aligned — see :func:`compare.page._render_job_group`.

Pure: no NiceGUI, no network — unit-testable in isolation, mirroring
:mod:`compare.selection`. The renderer in :mod:`compare.page` supplies
the participant labels (run name in workflow mode, scenario name when
the same machinery powers the workflow side of Compare Scenarios).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowStep:
    """One workflow step (member tool) aligned across the compared runs.

    ``job_ids`` has one entry per compared run, in the order the runs
    were supplied to :func:`align_workflow_runs`; ``None`` marks a run
    whose definition version doesn't include this step.
    """

    tool_alias: str
    tool_name: str
    position: int
    job_ids: tuple[int | None, ...]


@dataclass(frozen=True)
class WorkflowAlignment:
    """The result of aligning several runs of one workflow definition.

    ``versions`` carries each run's ``workflow_version_snapshot`` in
    run order; ``version_mix`` is ``True`` when more than one distinct
    (non-empty) snapshot appears, which the header flags so the user
    knows differing steps may be a version artefact rather than a real
    result difference.
    """

    workflow_alias: str
    workflow_name: str
    version_mix: bool
    versions: tuple[str, ...]
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True)
class WorkflowAlignmentError:
    """A user-facing reason the supplied runs can't be compared.

    Bundled (not raised) so the page renders :attr:`message` inline and
    the user can adjust the selection without the URL bouncing — the
    same soft-failure pattern :class:`compare.selection.SelectionError`
    uses.
    """

    message: str


def align_workflow_runs(
    runs: list[dict[str, Any]],
) -> WorkflowAlignment | WorkflowAlignmentError:
    """Align several workflow-run payloads into per-step comparison groups.

    Returns a :class:`WorkflowAlignmentError` when the runs can't be
    compared (fewer than two, or of different workflow definitions —
    cross-definition comparison isn't supported, per G36 Q4). Otherwise
    returns a :class:`WorkflowAlignment` whose ``steps`` are the union
    of every member tool across the runs, ordered by workflow position
    then tool name, each carrying one (possibly ``None``) job id per run.
    """
    if len(runs) < 2:
        return WorkflowAlignmentError(
            message="Pick at least two workflow runs to compare them.",
        )
    aliases = {str(r.get("workflow_alias") or "") for r in runs}
    if len(aliases) > 1:
        return WorkflowAlignmentError(
            message=(
                "These runs are of different workflows. Workflow comparison "
                "only works across runs of the same workflow — pick runs of "
                "one workflow, or use Scenario comparison instead."
            )
        )
    workflow_alias = next(iter(aliases))
    workflow_name = str(runs[0].get("workflow_name") or workflow_alias)
    versions = tuple(str(r.get("workflow_version_snapshot") or "") for r in runs)
    version_mix = len({v for v in versions if v}) > 1

    # Walk every run's children, building (a) a representative
    # (position, name) per tool for ordering, and (b) a per-run
    # ``tool_alias -> child job id`` lookup. A tool appears at most once
    # per run (one WorkflowMember per (workflow, tool)), so the lookup
    # never collides within a run.
    order_key: dict[str, tuple[int, str]] = {}
    child_by_run: list[dict[str, int]] = []
    for run in runs:
        by_tool: dict[str, int] = {}
        for child in run.get("children") or []:
            alias = str(child.get("tool_alias") or "")
            child_id = child.get("id")
            if not alias or child_id is None:
                continue
            by_tool[alias] = int(child_id)
            position = int(child.get("workflow_position") or 0)
            name = str(child.get("tool_name") or alias)
            existing = order_key.get(alias)
            if existing is None or position < existing[0]:
                order_key[alias] = (position, name)
        child_by_run.append(by_tool)

    ordered_aliases = sorted(
        order_key,
        key=lambda alias: (order_key[alias][0], order_key[alias][1], alias),
    )
    steps = tuple(
        WorkflowStep(
            tool_alias=alias,
            tool_name=order_key[alias][1],
            position=order_key[alias][0],
            job_ids=tuple(by_tool.get(alias) for by_tool in child_by_run),
        )
        for alias in ordered_aliases
    )
    return WorkflowAlignment(
        workflow_alias=workflow_alias,
        workflow_name=workflow_name,
        version_mix=version_mix,
        versions=versions,
        steps=steps,
    )
