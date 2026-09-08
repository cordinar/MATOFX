"""API tests for the cohort coverage endpoint (ADR-0020 §12 / §16, slice 3).

Two gates apply, and they are different: ``surrogate_family`` makes the whole
cohorts surface absent, while ``design_space_coverage`` makes *this* route
absent on an install that has cohorts but not coverage. Per
``feedback_per_customer_feature_toggles`` an off feature 404s rather than
403s — absent, not forbidden.
"""

from __future__ import annotations

import itertools
import pathlib
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from matofx.api.deps import get_settings
from matofx.audit import AuditWriter
from matofx.config import FeatureSettings, Settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature
from matofx.hierarchy import make_project_service, make_scenario_service
from matofx.hierarchy.orm import Job
from matofx.studies.orm import StudyResult
from matofx.tools import make_tool_service
from matofx.tools.loader import install_from_path
from matofx.tools.orm import ToolVersion

_SEQ = itertools.count(1)


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


async def _scenario_id(session, user_id: int) -> int:
    """A fresh project + scenario — a Job needs exactly one home."""
    project = await make_project_service(_audit()).create(
        session, actor_user_id=user_id, name=f"CovP-{next(_SEQ)}"
    )
    scenario = await make_scenario_service(_audit()).create(
        session, actor_user_id=user_id, project_id=project.id, name=f"CovS-{next(_SEQ)}"
    )
    return int(scenario.id)


def _enable(app: FastAPI, api_settings: Settings, *features: Feature) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[f.value for f in features])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _tool_version_id(api_factory: SessionFactory, tool_alias: str) -> int:
    async with session_scope(api_factory) as session:
        return int(
            (
                await session.execute(
                    select(ToolVersion.id)
                    .where(ToolVersion.alias == tool_alias)
                    .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                    .limit(1)
                )
            ).scalar_one()
        )


async def _cohort_with_runs(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    rows: list[dict[str, float]],
) -> int:
    """A cohort plus ``rows`` completed member runs with results."""
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    created = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={
            "name": "coverage cohort",
            "tool_alias": seeded["tool_alias"],
            "tolerated_tool_version_ids": [vid],
        },
    )
    assert created.status_code == 201, created.text
    cohort_id = int(created.json()["id"])

    async with session_scope(api_factory) as session:
        for i, row in enumerate(rows):
            job = Job(
                scenario_id=await _scenario_id(session, seeded["user_id"]),
                name=f"run-{i}",
                tool_alias=seeded["tool_alias"],
                tool_version_id=vid,
                state="complete",
                inputs=dict(row),
                created_by_user_id=seeded["user_id"],
            )
            session.add(job)
            await session.flush()
            session.add(
                StudyResult(
                    source_kind="cohort",
                    source_id=cohort_id,
                    job_id=job.id,
                    scalars={"y": float(i)},
                    retained_files=[],
                    validity="valid",
                    validity_reasons=[],
                )
            )
        await session.flush()
    return cohort_id


def _grid() -> list[dict[str, float]]:
    return [
        {"thermal_conductivity": float(a), "thickness": float(b)}
        for a in (1.0, 3.0, 5.0, 7.0, 9.0)
        for b in (1.0, 3.0, 5.0, 7.0, 9.0)
    ]


# --- gating ---------------------------------------------------------------


async def test_route_absent_when_the_coverage_feature_is_off(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    # Cohorts on, coverage off: the cohort itself is reachable, the coverage
    # route is not.
    _enable(app, api_settings, Feature.SURROGATE_FAMILY)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    assert (await client.get(f"/api/cohorts/{cohort_id}", headers=auth_headers)).status_code == 200
    resp = await client.post(f"/api/cohorts/{cohort_id}/coverage", headers=auth_headers, json={})
    assert resp.status_code == 404


async def test_route_absent_when_the_cohort_feature_is_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post("/api/cohorts/1/coverage", headers=auth_headers, json={})
    assert resp.status_code == 404


# --- the happy path -------------------------------------------------------


async def test_coverage_returns_a_full_report(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    resp = await client.post(f"/api/cohorts/{cohort_id}/coverage", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["verdict"] in ("good", "thin", "poor", "insufficient")
    assert body["criterion"] == "model_free"
    assert body["source_kind"] == "cohort"
    assert body["message"]
    assert body["per_response"] == []
    assert body["diagnostics"]["n_points"] == 25
    assert {f["name"] for f in body["envelope"]} == {"thermal_conductivity", "thickness"}
    assert {f["name"] for f in body["per_factor"]} == {"thermal_conductivity", "thickness"}


async def test_the_envelope_comes_back_editable(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    # The form is pre-populated from the observed spread and the user then
    # adjusts it — so the response has to carry what to pre-populate with.
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    body = (
        await client.post(f"/api/cohorts/{cohort_id}/coverage", headers=auth_headers, json={})
    ).json()
    axis = next(f for f in body["envelope"] if f["name"] == "thermal_conductivity")
    assert axis["kind"] == "continuous"
    assert axis["lo"] == 1.0
    assert axis["hi"] == 9.0
    assert axis["scale"] == "linear"
    assert axis["actionable"] is True


async def test_overrides_are_applied(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage",
        headers=auth_headers,
        json={"envelope": [{"name": "thermal_conductivity", "lo": 3.0, "hi": 7.0}]},
    )
    assert resp.status_code == 200, resp.text
    axis = next(f for f in resp.json()["envelope"] if f["name"] == "thermal_conductivity")
    assert (axis["lo"], axis["hi"]) == (3.0, 7.0)
    assert resp.json()["diagnostics"]["n_outside_envelope"] == 10


async def test_an_inverted_override_is_a_clean_400(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    # Plain-English, safe to show the user — not a 500.
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage",
        headers=auth_headers,
        json={"envelope": [{"name": "thermal_conductivity", "lo": 9.0, "hi": 1.0}]},
    )
    assert resp.status_code == 400, resp.text
    assert "thermal_conductivity" in resp.json()["detail"]


async def test_a_log_scale_override_is_accepted(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage",
        headers=auth_headers,
        json={"envelope": [{"name": "thermal_conductivity", "scale": "log"}]},
    )
    assert resp.status_code == 200, resp.text
    axis = next(f for f in resp.json()["envelope"] if f["name"] == "thermal_conductivity")
    assert axis["scale"] == "log"


async def test_an_unknown_cohort_is_a_404(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],  # noqa: ARG001
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    resp = await client.post("/api/cohorts/999999/coverage", headers=auth_headers, json={})
    assert resp.status_code == 404


async def test_an_empty_cohort_is_insufficient_not_an_error(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, [])

    resp = await client.post(f"/api/cohorts/{cohort_id}/coverage", headers=auth_headers, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "insufficient"
    assert resp.json()["message"]


async def test_the_scatter_payload_carries_the_points(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    # The pairwise scatter matrix is the visual payoff (§11.4), so the
    # response has to carry the training points, not just the verdict.
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _grid())

    body = (
        await client.post(f"/api/cohorts/{cohort_id}/coverage", headers=auth_headers, json={})
    ).json()
    assert len(body["points"]) == 25
    assert set(body["points"][0]) == {"thermal_conductivity", "thickness"}


# --- §11.5 suggesting, and committing --------------------------------------


_HEAT_SINK_ROWS: tuple[dict[str, float], ...] = (
    *(
        {"fin_height": 10.0 + i * 2.0, "fin_thickness": 1.0 + j * 0.2}
        for i in range(4)
        for j in range(4)
    ),
    {"fin_height": 55.0, "fin_thickness": 3.8},
)
"""A clustered design over **real** ``heat_sink`` inputs.

The other tests here seed invented column names, which the coverage engine is
happy with -- it infers factors from the data. Anything that reaches the
*generator* is not: submitting validates the factor spec against the pinned
descriptor, so a study built from invented names is rejected. `heat_sink` is
also the right tool for this: seven plain floats and **no sum-check**, where
`material_properties` constrains its three percentages to total 100 and a
free sweep of two of them would be skipped as invalid points -- testing the
generator's constraint handling rather than whether the batch ran."""


async def _heat_sink_cohort(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> tuple[int, int]:
    """``(cohort_id, project_id)`` for a heat-sink cohort with real runs."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    async with session_scope(api_factory) as session:
        installed = await install_from_path(
            session,
            make_tool_service(_audit()),
            actor_user_id=seeded["user_id"],
            bundle_dir=repo_root / "example_tools" / "heat_sink",
        )
        service = make_tool_service(_audit())
        for state in ("test", "released"):
            await service.promote_state(
                session, actor_user_id=seeded["user_id"], alias=installed.alias, new_state=state
            )
        alias = installed.alias

    vid = await _tool_version_id(api_factory, alias)
    created = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={
            "name": "heat sink cohort",
            "tool_alias": alias,
            "tolerated_tool_version_ids": [vid],
        },
    )
    assert created.status_code == 201, created.text
    cohort_id = int(created.json()["id"])

    async with session_scope(api_factory) as session:
        for i, row in enumerate(_HEAT_SINK_ROWS):
            job = Job(
                # A fresh scenario per job: `job` is unique on
                # (scenario_id, tool_alias), so they cannot share one.
                scenario_id=await _scenario_id(session, seeded["user_id"]),
                name=f"hs-{i}",
                tool_alias=alias,
                tool_version_id=vid,
                state="complete",
                inputs=dict(row),
                created_by_user_id=seeded["user_id"],
            )
            session.add(job)
        await session.flush()
        project_id = await _project_id(session, seeded["user_id"])

    return cohort_id, int(project_id)


def _clustered() -> list[dict[str, float]]:
    """A design with a large hole, so there is something to suggest."""
    rows = [
        {"thermal_conductivity": 1.0 + i * 0.2, "thickness": 1.0 + j * 0.2}
        for i in range(4)
        for j in range(4)
    ]
    rows.append({"thermal_conductivity": 9.0, "thickness": 9.0})
    return rows


async def test_the_plan_prices_several_batch_sizes(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """"How many runs do I need?" is the question -- not one to ask the user.

    The panel used to offer a spinner labelled "How many runs", which asks the
    user for the very thing they opened the panel to find out. The plan prices
    a ladder of sizes against the same measurement the verdict uses, so the
    choice can be read off the curve.
    """
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage/plan",
        headers=auth_headers,
        json={"sizes": [2, 4, 8]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [o["n"] for o in body["options"]] == [2, 4, 8]
    # The gap is the monotone quantity -- it is what the table leads with,
    # precisely because the efficiency score need not improve with size.
    gaps = [o["gap_after"] for o in body["options"]]
    assert gaps == sorted(gaps, reverse=True)
    assert all(g <= body["current_gap"] + 1e-9 for g in gaps)

    # The runs themselves ride along so choosing a size needs no round trip
    # and the user can read the values before committing anything.
    assert len(body["points"]) >= 8
    assert all({"thermal_conductivity", "thickness"} <= set(p) for p in body["points"])
    # ...headed by labels, never internal names.
    assert set(body["factor_labels"]) >= {"thermal_conductivity", "thickness"}


async def test_the_plan_marks_at_most_one_batch_as_enough(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The knee of the curve, and only when there is a threshold left to cross.

    Without it every row looks an equally good buy, which is how somebody
    commissions twenty runs to gain what eight would have. An archive already
    reading ``good`` has no threshold left, so no row may claim one.
    """
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())

    body = (
        await client.post(
            f"/api/cohorts/{cohort_id}/coverage/plan",
            headers=auth_headers,
            json={"sizes": [2, 4, 8]},
        )
    ).json()

    flagged = [o for o in body["options"] if o["reaches_good"]]
    assert len(flagged) <= 1
    if body["already_good"]:
        assert not flagged


async def test_the_plan_is_absent_when_the_coverage_feature_is_off(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())
    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage/plan", headers=auth_headers, json={}
    )
    assert resp.status_code == 404


async def test_the_report_carries_a_label_for_every_axis(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Labels reach the wire, or the panel has nothing to render but names.

    [[feedback_use_labels_not_names]] -- the rule the coverage panel was
    breaking on every row, every warning and every plot axis.
    """
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())

    body = (
        await client.post(
            f"/api/cohorts/{cohort_id}/coverage", headers=auth_headers, json={}
        )
    ).json()

    assert body["envelope"]
    assert all("label" in f for f in body["envelope"])
    assert all("label" in f for f in body["per_factor"])


async def test_suggest_returns_a_batch_with_no_model_fitted(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage/suggest", headers=auth_headers, json={"n": 4}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["points"]) == 4
    # No response has been chosen, so the batch must not claim one.
    assert body["response_name"] == ""
    assert all({"thermal_conductivity", "thickness"} <= set(p) for p in body["points"])


async def test_suggest_is_absent_when_the_coverage_feature_is_off(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())

    for route in ("coverage/suggest", "coverage/study"):
        resp = await client.post(
            f"/api/cohorts/{cohort_id}/{route}", headers=auth_headers, json={"n": 3}
        )
        assert resp.status_code == 404, route


async def test_the_batch_becomes_a_running_study(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The button runs the batch; it does not park a draft (§11.5).

    It used to leave the study at ``draft``, which read as "Not submitted yet"
    on a page the user had just been sent to expressly to watch it run. The
    batch has already had every review a draft would get -- priced, listed run
    by run, and committed by a button naming the cohort it feeds -- so the
    extra manual submit was a step with nothing left to decide.

    The tool's own ``approvals_policy`` still decides what happens next; this
    fixture's tool needs none, so the points fan out and queue.
    """
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id, project_id = await _heat_sink_cohort(
        client, auth_headers, api_factory, seeded
    )

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage/study",
        headers=auth_headers,
        json={"n": 4, "project_id": project_id, "name": "Fill the gaps"},
    )
    assert resp.status_code == 201, resp.text
    study = resp.json()
    assert study["state"] == "running", "the study was parked instead of run"
    assert study["mode"] == "table"
    assert study["name"] == "Fill the gaps"
    assert study["generated_count"] == 4

    async with session_scope(api_factory) as session:
        children = (
            await session.execute(
                select(Job.state).where(Job.parametric_study_id == int(study["id"]))
            )
        ).scalars().all()
    assert len(children) == 4, "no child runs were created"
    assert set(children) == {"queued"}, f"children were not queued: {set(children)}"


async def test_the_commissioned_study_is_linked_to_the_cohort(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Running them is only half of it -- they have to come back (§11.5)."""
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id, project_id = await _heat_sink_cohort(
        client, auth_headers, api_factory, seeded
    )

    study = (
        await client.post(
            f"/api/cohorts/{cohort_id}/coverage/study",
            headers=auth_headers,
            json={"n": 3, "project_id": project_id, "name": "Fill the gaps"},
        )
    ).json()

    cohort = (await client.get(f"/api/cohorts/{cohort_id}", headers=auth_headers)).json()
    assert cohort["auto_include_study_ids"] == [int(study["id"])]


async def test_a_batch_size_of_zero_is_rejected_by_validation(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, _clustered())

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage/suggest", headers=auth_headers, json={"n": 0}
    )
    assert resp.status_code == 422


async def test_nothing_to_suggest_is_a_clean_400_not_an_empty_study(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable(app, api_settings, Feature.SURROGATE_FAMILY, Feature.DESIGN_SPACE_COVERAGE)
    rows = [{"thermal_conductivity": 2.0, "thickness": 3.0} for _ in range(8)]
    cohort_id = await _cohort_with_runs(client, auth_headers, api_factory, seeded, rows)
    async with session_scope(api_factory) as session:
        project_id = await _project_id(session, seeded["user_id"])

    resp = await client.post(
        f"/api/cohorts/{cohort_id}/coverage/study",
        headers=auth_headers,
        json={"n": 3, "project_id": project_id, "name": "empty"},
    )
    assert resp.status_code == 400, resp.text
    assert "suggest" in resp.json()["detail"].lower()


async def _project_id(session, user_id: int) -> int:
    """A project the caller can create studies in."""
    project = await make_project_service(_audit()).create(
        session, actor_user_id=user_id, name=f"CovStudy-{next(_SEQ)}"
    )
    return int(project.id)
