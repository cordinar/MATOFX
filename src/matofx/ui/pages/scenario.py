"""Scenario view (``/ui/scenarios/{scenario_id}``).

Lists jobs in a scenario + offers a "Submit new job" button. Click a
job to drop into its result page.
"""

from __future__ import annotations

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

# State badge colours — match the canonical state vocabulary.
_STATE_BADGE: dict[str, str] = {
    "created": "color=grey",
    "queued": "color=info",
    "running": "color=warning",
    "complete": "color=positive",
    "failed": "color=negative",
}


@ui.page("/scenarios/{scenario_id}")
async def scenario_page(scenario_id: int) -> None:
    """Render the jobs list for one scenario."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        scenario = await client.get_scenario(scenario_id)
        jobs = await client.list_jobs(scenario_id)
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
            ui.label(f"We couldn't load that scenario: {exc.detail}").style("color: #c0392b;")
        return

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        project_id = scenario["project_id"]
        ui.button(
            "← Back to project",
            on_click=lambda: ui.navigate.to(f"/projects/{project_id}"),
        ).props("flat")
        ui.label(str(scenario["name"])).style("font-size: 1.5rem; font-weight: 600;")
        if scenario.get("description"):
            ui.label(str(scenario["description"])).style("color: #555;")

        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Jobs").style("font-size: 1.2rem; font-weight: 600;")
            ui.button(
                "Submit new job",
                on_click=lambda: ui.navigate.to(f"/scenarios/{scenario_id}/submit"),
            ).props("color=primary")

        if not jobs:
            ui.label("No jobs in this scenario yet. Click 'Submit new job' to start one.").style(
                "color: #555;"
            )
            return

        for job in jobs:
            _render_job_row(job)


def _render_job_row(job: dict[str, object]) -> None:
    jid = job["id"]
    state = str(job.get("state") or "")
    with (
        ui.card()
        .classes("w-full cursor-pointer hover:bg-gray-50")
        .on("click", lambda _e=None, _jid=jid: ui.navigate.to(f"/jobs/{_jid}"))
    ):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(str(job["name"])).style("font-size: 1.05rem; font-weight: 600;")
                ui.label(f"Tool: {job.get('tool_alias') or '-'}").style(
                    "color: #555; font-size: 0.85rem;"
                )
            ui.badge(state.upper() or "?").props(_STATE_BADGE.get(state, "color=grey"))
