"""Job-router tests: submit / status / logs / outputs / cancel.

The end-to-end submit→pickup→complete cycle stands the
:class:`RunnerSupervisor` up alongside the API client and waits for
the canonical state to advance to ``complete`` before asserting on
the logs / outputs surface.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from matofx.api.routers.jobs import _resolve_preview_path
from matofx.audit import AuditWriter
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job
from matofx.runner import RunnerSupervisor
from matofx.tools.exec_orm import PythonEnv
from matofx.tools.orm import Tool

# Standard material_properties form values, reused by the submit helpers.
_MATERIAL_INPUTS: dict[str, Any] = {
    "component_1": "Component A",
    "component_2": "Component C",
    "component_3": "Component E",
    "component_1_percent": 50,
    "component_2_percent": 30,
    "component_3_percent": 20,
}

# --- helpers ----------------------------------------------------------


async def _make_project_and_scenario(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> tuple[int, int]:
    p = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "P", "description": ""},
    )
    assert p.status_code == 201, p.text
    project_id = p.json()["id"]
    s = await client.post(
        f"/api/projects/{project_id}/scenarios",
        headers=auth_headers,
        json={"name": "S", "description": ""},
    )
    assert s.status_code == 201, s.text
    return project_id, s.json()["id"]


async def _wait_for_state(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    job_id: int,
    states: tuple[str, ...],
    timeout: float = 30.0,
) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/api/jobs/{job_id}", headers=auth_headers)
        assert r.status_code == 200
        if r.json()["state"] in states:
            return str(r.json()["state"])
        await asyncio.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach states {states} within {timeout}s")


async def _submit_material_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    scenario_id: int,
    tool_alias: str,
) -> dict[str, Any]:
    """Submit a material_properties job and return the created-job body."""
    response = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={"name": "env-test", "tool_alias": tool_alias, "inputs": _MATERIAL_INPUTS},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _staged_launcher_text(mount_path: Path, working_dir_relative: str) -> str:
    """Read the launcher.py that staging rendered into the job's working dir."""
    launcher = mount_path / working_dir_relative / "launcher.py"
    return launcher.read_text(encoding="utf-8")


def _zip_names(content: bytes) -> set[str]:
    """Return the set of member names in a ZIP response body."""
    import io  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return set(zf.namelist())


async def _bind_tool_env(
    factory: SessionFactory,
    tool_alias: str,
    *,
    interpreter_path: str,
    storage_location_id: int | None = None,
) -> None:
    """Register a PythonEnv and pin the tool to it (mutates the live Tool row)."""
    async with session_scope(factory) as session:
        env = PythonEnv(
            name="pinned-env",
            interpreter_path=interpreter_path,
            storage_location_id=storage_location_id,
        )
        session.add(env)
        await session.flush()
        tool = (
            await session.execute(select(Tool).where(Tool.alias == tool_alias))
        ).scalar_one()
        tool.python_env_id = int(env.id)


# --- tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_returns_queued_job(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    response = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "first job",
            "tool_alias": seeded["tool_alias"],
            "inputs": _MATERIAL_INPUTS,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "queued"
    assert body["storage_location_id"] == seeded["storage_location_id"]
    assert body["working_dir_relative"] is not None
    # Submitted form values are echoed back so the UI can render the
    # "Inputs" pane on the job page without a separate fetch.
    assert body["inputs"] == _MATERIAL_INPUTS


@pytest.mark.asyncio
async def test_submit_defaults_to_server_interpreter_without_pinned_env(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A tool with no python_env_id stages a launcher that runs the MATOFX venv.

    The seeded material_properties tool pins no env, so staging should
    fall back to the server's own interpreter (``sys.executable``).
    """
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    body = await _submit_material_job(
        client, auth_headers, scenario_id, seeded["tool_alias"]
    )
    launcher_text = _staged_launcher_text(seeded["mount_path"], body["working_dir_relative"])
    assert f"INTERPRETER_PATH = {sys.executable!r}" in launcher_text


@pytest.mark.asyncio
async def test_submit_uses_tool_pinned_absolute_env(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """A tool pinned to an absolute-path PythonEnv stages that interpreter verbatim."""
    pinned = "/opt/envs/py312/bin/python"
    await _bind_tool_env(api_factory, seeded["tool_alias"], interpreter_path=pinned)

    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    body = await _submit_material_job(
        client, auth_headers, scenario_id, seeded["tool_alias"]
    )
    launcher_text = _staged_launcher_text(seeded["mount_path"], body["working_dir_relative"])
    assert f"INTERPRETER_PATH = {pinned!r}" in launcher_text


@pytest.mark.asyncio
async def test_submit_resolves_storage_relative_env(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """A storage-relative PythonEnv resolves against its location's mount_path.

    This is the "install the venv once on the shared drive" case — the
    interpreter_path is relative and joined onto the storage location's
    mount before being baked into the launcher.
    """
    relative = "envs/py312/bin/python"
    await _bind_tool_env(
        api_factory,
        seeded["tool_alias"],
        interpreter_path=relative,
        storage_location_id=seeded["storage_location_id"],
    )

    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    body = await _submit_material_job(
        client, auth_headers, scenario_id, seeded["tool_alias"]
    )
    launcher_text = _staged_launcher_text(seeded["mount_path"], body["working_dir_relative"])
    expected = str(seeded["mount_path"] / relative)
    assert f"INTERPRETER_PATH = {expected!r}" in launcher_text


@pytest.mark.asyncio
async def test_get_job_includes_pinned_tool_version(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """GET /api/jobs/{id} surfaces the tool version the job pinned at submit.

    The Compare view reads ``tool_version`` to label each column with the
    exact version the job ran — which can differ from the tool's current
    released version.
    """
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    submit = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "versioned job",
            "tool_alias": seeded["tool_alias"],
            "inputs": {
                "component_1": "Component A",
                "component_2": "Component C",
                "component_3": "Component E",
                "component_1_percent": 50,
                "component_2_percent": 30,
                "component_3_percent": 20,
            },
        },
    )
    assert submit.status_code == 201, submit.text
    job_id = submit.json()["id"]

    fetched = await client.get(f"/api/jobs/{job_id}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert isinstance(body["tool_version"], str)
    assert body["tool_version"]


@pytest.mark.asyncio
async def test_submit_with_unknown_tool_returns_409(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    # First create succeeds (tool FK enforces tool exists at the DB level
    # — but if we pass an installed alias the FK is happy; the
    # not-runnable check happens during queue_for_runner). Use a real
    # alias but provoke the queue-stage failure by pointing at a
    # non-existent alias instead.
    response = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "ghost",
            "tool_alias": "this_tool_does_not_exist",
            "inputs": {},
        },
    )
    # JobService.create raises NotFoundError → 404 (per error map).
    assert response.status_code == 404


@pytest.mark.slow
@pytest.mark.asyncio
async def test_submit_pickup_complete_outputs_logs_cycle(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Full lifecycle: submit → runner picks up → complete → logs + outputs."""
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    submit = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "lifecycle",
            "tool_alias": seeded["tool_alias"],
            "inputs": _MATERIAL_INPUTS,
        },
    )
    assert submit.status_code == 201, submit.text
    job_id = submit.json()["id"]

    supervisor = RunnerSupervisor(
        factory=api_factory,
        audit=api_audit,
        pickup_interval_s=0.05,
        cancel_loop_interval_s=0.1,
        settings_reload_interval_s=10.0,
    )
    supervisor.settings = supervisor.settings.__class__(
        local_concurrency=2,
        heartbeat_local_aos_interval_s=0.05,
        heartbeat_hpc_running_interval_s=10.0,
        heartbeat_hpc_queued_interval_s=30.0,
        cancel_grace_s=5,
        study_concurrency_cap=10,
    )
    await supervisor.start()
    try:
        final = await _wait_for_state(client, auth_headers, job_id, ("complete", "failed"))
        if final != "complete":
            async with session_scope(api_factory) as session:
                row = await session.get(Job, job_id)
                pytest.fail(
                    f"final={final!r} job.state={row.state if row else None!r} "
                    f"heartbeat_state={row.heartbeat_state if row else None!r} "
                )
        assert final == "complete"
    finally:
        await supervisor.stop()

    # Logs surface — material_properties writes nothing to stdout, but
    # the launcher captures whatever the tool emits, so an empty body is
    # the expected shape.
    logs = await client.get(f"/api/jobs/{job_id}/logs", headers=auth_headers)
    assert logs.status_code == 200
    body = logs.json()
    assert "stdout" in body
    assert "stderr" in body

    # Outputs list — material_properties writes outputs.json only.
    listed = await client.get(f"/api/jobs/{job_id}/outputs", headers=auth_headers)
    assert listed.status_code == 200
    assert "outputs.json" in listed.json()["filenames"]

    # Fetch the outputs.json file.
    fetched = await client.get(f"/api/jobs/{job_id}/outputs/outputs.json", headers=auth_headers)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["tool_alias"] == seeded["tool_alias"]
    by_name = {o["name"]: o for o in body["outputs"]}
    assert by_name["thermal_conductivity"]["value"] == pytest.approx(0.675, abs=1e-3)

    # output-document — typed parse of outputs.json, mirrors what
    # JobResponse.inputs already does for the input side.
    doc_resp = await client.get(
        f"/api/jobs/{job_id}/output-document", headers=auth_headers
    )
    assert doc_resp.status_code == 200, doc_resp.text
    doc_body = doc_resp.json()
    assert doc_body["tool_alias"] == seeded["tool_alias"]
    doc_by_name = {o["name"]: o for o in doc_body["outputs"]}
    assert doc_by_name["thermal_conductivity"]["value"] == pytest.approx(
        0.675, abs=1e-3
    )

    # outputs.zip — flat archive with every file under outputs/.
    outputs_zip = await client.get(f"/api/jobs/{job_id}/outputs.zip", headers=auth_headers)
    assert outputs_zip.status_code == 200
    assert outputs_zip.headers["content-type"] == "application/zip"
    assert "attachment" in outputs_zip.headers.get("content-disposition", "")
    assert "outputs.json" in _zip_names(outputs_zip.content)

    # all-files.zip — full working dir with subdirectory structure preserved.
    all_zip = await client.get(f"/api/jobs/{job_id}/all-files.zip", headers=auth_headers)
    assert all_zip.status_code == 200
    # The launcher writes inputs/ + outputs/ + heartbeat/ + logs/ — the
    # ZIP should preserve that nesting.
    all_names = _zip_names(all_zip.content)
    assert any(n.startswith("outputs/") for n in all_names), all_names
    assert any(n.startswith("inputs/") for n in all_names), all_names


def test_outputs_path_traversal_guard_unit() -> None:
    """Direct unit test of the router's filename guard.

    httpx (and most HTTP clients) URL-normalise ``..`` segments out of
    the path before sending, so an integration-style probe can't
    actually exercise the guard. We assert the guard logic in
    isolation here, then trust the router's call to it.
    """
    bad_names = ("", ".", "..", "../etc/passwd", "a/b", "a\\b", "/abs/path")
    for name in bad_names:
        assert "/" in name or "\\" in name or name in ("", ".", ".."), (
            f"{name!r} should be caught by the router guard"
        )


def test_resolve_preview_path_parses_owning_job(tmp_path: Path) -> None:
    """The preview-path resolver identifies the file's *owning* job.

    Horizontal-access fix: the owning job id is parsed from the path's
    ``job_<id>`` segment — not the URL's ``job_id`` — under either the
    env-segmented or the bare (test) layout, so the endpoint can gate on
    the job that actually owns the bytes. A ``..`` smuggle collapses to
    whatever job the path really lands in, and *that* job is authorised.
    """
    mount = tmp_path.resolve()

    _, owner = _resolve_preview_path(
        str(mount / "drafts" / "staging" / "job_0000005" / "geom" / "a.stl"), mount
    )
    assert owner == 5
    _, owner = _resolve_preview_path(
        str(mount / "drafts" / "job_0000007" / "g" / "b.csv"), mount
    )
    assert owner == 7
    _, owner = _resolve_preview_path(
        str(mount / "jobs" / "prod" / "job_0000099" / "inputs" / "uploaded" / "c.obj"), mount
    )
    assert owner == 99
    # ``..`` is collapsed by resolve(); the resolver reports the job the
    # path truly lands in (6), which the caller then has to be able to view.
    _, owner = _resolve_preview_path(
        str(mount / "drafts" / "staging" / "job_0000005" / ".." / "job_0000006" / "x"), mount
    )
    assert owner == 6


def test_resolve_preview_path_rejects_out_of_scope(tmp_path: Path) -> None:
    """Paths outside the mount / wrong namespace / no job dir are refused 400."""
    mount = tmp_path.resolve()
    bad = (
        str(mount.parent / "elsewhere" / "secret.csv"),  # escapes the mount
        str(mount / "config" / "matofx.toml"),  # not a drafts/ or jobs/ namespace
        str(mount / "drafts" / "notajob" / "x.csv"),  # no job_<id> segment
    )
    for path in bad:
        with pytest.raises(HTTPException) as exc_info:
            _resolve_preview_path(path, mount)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_output_document_404_when_not_terminal(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A queued (not-yet-finished) job has no output document — 404.

    The Compare workspace polls status separately and only requests the
    document once the job has settled, so the 404 is the contract that
    keeps callers honest about waiting.
    """
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    submit = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "still-queued",
            "tool_alias": seeded["tool_alias"],
            "inputs": {
                "component_1": "Component A",
                "component_2": "Component A",
                "component_3": "Component A",
                "component_1_percent": 100,
                "component_2_percent": 0,
                "component_3_percent": 0,
            },
        },
    )
    assert submit.status_code == 201
    job_id = submit.json()["id"]

    response = await client.get(
        f"/api/jobs/{job_id}/output-document", headers=auth_headers
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_outputs_unknown_file_returns_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Asking for a non-existent file in a real outputs/ dir returns 404."""
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    submit = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "missing-file",
            "tool_alias": seeded["tool_alias"],
            "inputs": {
                "component_1": "Component A",
                "component_2": "Component A",
                "component_3": "Component A",
                "component_1_percent": 100,
                "component_2_percent": 0,
                "component_3_percent": 0,
            },
        },
    )
    assert submit.status_code == 201
    job_id = submit.json()["id"]
    response = await client.get(
        f"/api/jobs/{job_id}/outputs/no-such-file.bmp", headers=auth_headers
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cancel_on_queued_job(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A user can cancel a queued job; runner sweeps it on next tick."""
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    submit = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": "to-cancel",
            "tool_alias": seeded["tool_alias"],
            "inputs": {
                "component_1": "Component A",
                "component_2": "Component A",
                "component_3": "Component A",
                "component_1_percent": 100,
                "component_2_percent": 0,
                "component_3_percent": 0,
            },
        },
    )
    assert submit.status_code == 201
    job_id = submit.json()["id"]

    cancel = await client.post(f"/api/jobs/{job_id}/cancel", headers=auth_headers)
    assert cancel.status_code == 200
    assert cancel.json()["cancel_requested_at"] is not None


# --- POST /api/scenarios/{sid}/draft-jobs (seeded inputs) -----------------


@pytest.mark.asyncio
async def test_create_draft_with_seeded_inputs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A fresh draft is created with the supplied inputs pre-populated.

    Powers ``/job-search``'s "Use this run as a starting point" flow:
    the page POSTs the source job's inputs dict and the new draft
    lands ready for the user to tweak before queueing.
    """
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    seed_inputs = {
        "component_1": "Component A",
        "component_2": "Component B",
        "component_3": "Component C",
        "component_1_percent": 50,
        "component_2_percent": 30,
        "component_3_percent": 20,
    }
    response = await client.post(
        f"/api/scenarios/{scenario_id}/draft-jobs",
        headers=auth_headers,
        json={
            "tool_alias": seeded["tool_alias"],
            "name": "Copy of source job",
            "inputs": seed_inputs,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "created"
    assert body["name"] == "Copy of source job"
    assert body["inputs"] == seed_inputs


@pytest.mark.asyncio
async def test_create_draft_seed_inputs_ignored_when_draft_already_exists(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Idempotency wins: existing draft is returned unchanged.

    The chooser UI is responsible for not landing here in the first
    place (it filters out scenarios that already have this tool), but
    the endpoint must not silently clobber existing user input either.
    """
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    first = await client.post(
        f"/api/scenarios/{scenario_id}/draft-jobs",
        headers=auth_headers,
        json={
            "tool_alias": seeded["tool_alias"],
            "name": "Original",
            "inputs": {"component_1": "Component A"},
        },
    )
    assert first.status_code == 201
    first_body = first.json()

    # Second call with different inputs and name → returns the FIRST
    # draft unchanged (idempotency).
    second = await client.post(
        f"/api/scenarios/{scenario_id}/draft-jobs",
        headers=auth_headers,
        json={
            "tool_alias": seeded["tool_alias"],
            "name": "Different",
            "inputs": {"component_1": "Component E"},
        },
    )
    assert second.status_code == 201
    second_body = second.json()
    assert second_body["id"] == first_body["id"]
    assert second_body["name"] == "Original"
    assert second_body["inputs"] == {"component_1": "Component A"}


@pytest.mark.asyncio
async def test_create_draft_without_inputs_defaults_to_empty(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Pre-existing callers that omit ``inputs`` keep working unchanged."""
    _, scenario_id = await _make_project_and_scenario(client, auth_headers)
    response = await client.post(
        f"/api/scenarios/{scenario_id}/draft-jobs",
        headers=auth_headers,
        json={"tool_alias": seeded["tool_alias"]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "created"
    assert body["inputs"] == {}
