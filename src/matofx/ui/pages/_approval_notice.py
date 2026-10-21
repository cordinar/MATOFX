"""The read-only "this run is approval-gated" notice (§16.15).

Shared by every surface that can land on one of the four approval
states, so the wording is identical wherever a user meets it: the My
Jobs right pane and a workflow run's per-tool tab.

It lived in ``my_jobs.py`` alone, which is how the workflow-run page
came to have no approval handling at all — its ``_tab_status_for``
recognised only the five original lifecycle states, so a job held for
approval fell through to ``"ready"`` and the tab cheerfully re-rendered
the *input form* for a run that had already been submitted (and, for a
``denied`` job, for a run that can never happen).

None of these surfaces ever *acts* on an approval — ``/approvals`` is
the only place to decide. This is explanation only, plus a shortcut for
a user who happens to be an approver themselves.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui.format import format_job_id, format_job_state
from matofx.ui.state import current_is_approver

#: ``Job.state`` → (headline, explanation). Keys are exactly the four
#: approval states in :data:`matofx.hierarchy.orm.JOB_STATES`.
APPROVAL_NOTICES: dict[str, tuple[str, str]] = {
    "pending_approval": (
        "Awaiting approval",
        "This simulation needs an approver's sign-off before it can run. "
        "The approver(s) have been notified; you'll be emailed when it's "
        "approved or denied.",
    ),
    "denied": (
        "Denied",
        "This simulation was denied and did not run. The approver's "
        "reason was sent to you by email.",
    ),
    "pending_results": (
        "Results awaiting approval",
        "This simulation finished, but its results are held for an "
        "approver to review before release. You'll be emailed when they "
        "are released.",
    ),
    "results_withheld": (
        "Results withheld",
        "An approver reviewed this simulation and withheld its results, "
        "so they aren't available. Contact an administrator if you "
        "believe this is a mistake.",
    ),
}

#: The states :func:`render_approval_notice` speaks for. A surface that
#: dispatches on ``Job.state`` should route these here rather than
#: letting them fall into a generic branch.
APPROVAL_STATES: frozenset[str] = frozenset(APPROVAL_NOTICES)


def is_approval_state(state: str) -> bool:
    """True when ``state`` is one of the four approval-gated job states."""
    return state in APPROVAL_STATES


def render_approval_notice(state: str, job: dict[str, Any] | None) -> None:
    """Read-only notice card for an approval-gated job."""
    title, body = APPROVAL_NOTICES.get(
        state, ("Awaiting approval", "This simulation is awaiting approval.")
    )
    job_id = int(job["id"]) if job and "id" in job else None
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.badge(format_job_state(state)).props("color=deep-orange")
            if job_id is not None:
                ui.label(format_job_id(job_id)).style("color: #888;")
            # Name it, like every other state now does — an approval
            # notice that only quotes an id makes the user go and look
            # up which run it is talking about.
            job_name = str((job or {}).get("name") or "")
            if job_name:
                ui.label(job_name).style("font-weight: 600;")
        ui.label(title).style("font-size: 1.1rem; font-weight: 600;")
        ui.label(body).style("color: #888;")
        if current_is_approver():
            ui.button(
                "Go to Approvals",
                on_click=lambda: ui.navigate.to("/approvals"),
            ).props("color=primary")
