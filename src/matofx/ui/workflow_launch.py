"""Shared "launch a workflow into a scenario" dialog.

A custom workflow and an admin-released workflow are the same
:class:`~matofx.workflows.orm.Workflow` row under the hood, so both the
Home catalog and the *My Custom Workflows* workspace launch them the
same way: pick a project + scenario (or add a fresh scenario inline),
then POST a workflow run via ``start_workflow_run`` and navigate to the
new run on ``/my-jobs``.

This module owns that dialog so the two surfaces don't drift. The
"one run per workflow per scenario" rule is surfaced by hiding
scenarios that already host a run of the chosen workflow — the user
clicks *New scenario* to add one when every existing scenario is taken.
"""

from __future__ import annotations

from nicegui import ui

from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.scenario_naming import next_scenario_name


def open_launch_in_existing_project_dialog(  # noqa: PLR0915 — single linear dialog builder
    client: ApiClient, workflow_alias: str, display_label: str
) -> None:
    """Pick an existing project + scenario, then start the workflow run.

    Scenarios listed are those without an existing run of this
    workflow on them (the "one run per workflow per scenario" rule
    enforced at the DB level). A "+ New scenario" affordance creates
    a fresh scenario inline.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label(f"Launch '{display_label}' in an existing project").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        ui.label(
            "Pick the project and scenario the workflow run belongs to. "
            "Scenarios that already have a run of this workflow are hidden "
            "— click 'New scenario' to add one."
        ).style("color: #888;")

        project_select = (
            ui.select(options={}, label="Project", with_input=True).classes("w-full").props("dense")
        )
        scenario_select = (
            ui.select(options={}, label="Scenario", with_input=True)
            .classes("w-full")
            .props("disable dense")
        )
        new_scenario_panel = ui.column().classes("w-full gap-1")
        new_scenario_panel.visible = False
        with new_scenario_panel:
            new_scenario_name = (
                ui.input("New scenario name", value="Scenario 1").props("dense").classes("w-full")
            )
        new_scenario_btn = ui.button("New scenario", icon="add").props(
            "flat dense color=primary disable"
        )

        message = ui.label("").style("color: #ff6b6b;")
        launch_btn = ui.button("Launch").props("color=primary disable").classes("w-32")

        async def _load_projects() -> None:
            try:
                projects = await client.list_projects()
            except ApiError as exc:
                message.text = exc.detail
                return
            project_select.options = {int(p["id"]): str(p["name"]) for p in projects}
            project_select.update()

        async def _load_scenarios() -> None:
            project_id = project_select.value
            scenario_select.value = None
            scenario_select.options = {}
            scenario_select.props("disable dense")
            new_scenario_btn.props("disable")
            new_scenario_panel.visible = False
            launch_btn.props("disable color=primary")
            if project_id is None:
                scenario_select.update()
                return
            try:
                scenarios = await client.list_scenarios(int(project_id))
            except ApiError as exc:
                message.text = exc.detail
                return
            available: dict[int, str] = {}
            for scenario in scenarios:
                sid = int(scenario["id"])
                try:
                    runs = await client.list_workflow_runs_for_scenario(sid)
                except ApiError:
                    runs = []
                # Filter scenarios that already host this workflow.
                if any(str(r.get("workflow_alias")) == workflow_alias for r in runs):
                    continue
                available[sid] = str(scenario.get("name") or "—")

            scenario_select.options = available
            # Suggest the next sequential name from *all* scenarios in the
            # project (not just the un-filtered ones) so the number reflects
            # what already exists and the user can usually just click through.
            new_scenario_name.value = next_scenario_name(
                str(s.get("name") or "") for s in scenarios
            )
            new_scenario_btn.props(remove="disable")
            if available:
                scenario_select.props(remove="disable")
            scenario_select.update()

        def _refresh_launch_state() -> None:
            if new_scenario_panel.visible:
                ok = bool((new_scenario_name.value or "").strip())
            else:
                ok = scenario_select.value is not None
            if ok:
                launch_btn.props(remove="disable")
            else:
                launch_btn.props("disable color=primary")

        async def _go() -> None:
            project_id = project_select.value
            if project_id is None:
                message.text = "Please pick a project."
                return
            scenario_id: int | None
            if new_scenario_panel.visible:
                name = (new_scenario_name.value or "").strip()
                if not name:
                    message.text = "Please enter a name for the new scenario."
                    return
                try:
                    scenario = await client.create_scenario(
                        int(project_id), name=name, description=""
                    )
                except ApiError as exc:
                    message.text = exc.detail
                    return
                scenario_id = int(scenario["id"])
            else:
                scenario_id = scenario_select.value
            if scenario_id is None:
                message.text = "Please pick a scenario."
                return
            try:
                run = await client.start_workflow_run(
                    int(scenario_id),
                    workflow_alias=workflow_alias,
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.navigate.to(f"/my-jobs?workflow_run={int(run['id'])}")

        def _show_new_panel() -> None:
            new_scenario_panel.visible = True
            scenario_select.value = None
            scenario_select.props("disable dense")
            scenario_select.update()
            _refresh_launch_state()

        launch_btn.on_click(_go)
        project_select.on_value_change(lambda _e: _load_scenarios())
        scenario_select.on_value_change(lambda _e: _refresh_launch_state())
        new_scenario_name.on_value_change(lambda _e: _refresh_launch_state())
        new_scenario_btn.on_click(_show_new_panel)

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

        ui.timer(0.05, _load_projects, once=True)
    dialog.open()
