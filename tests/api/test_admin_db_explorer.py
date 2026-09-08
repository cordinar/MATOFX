"""Tests for the Admin → Explore Database router.

Cover the full GET surface: listing the allowed tables, fetching a
page of rows with redaction applied, the allowlist (hidden tables
404), the sort + filter validation, pagination, and the permission
gate (regular user is 403, super_user / administrator pass).
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from matofx.db import session_scope
from matofx.db.explorer import REDACTION_PLACEHOLDER
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job


@pytest.mark.asyncio
async def test_list_tables_requires_auth(client: httpx.AsyncClient) -> None:
    """No bearer → 401."""
    r = await client.get("/api/admin/db/tables")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_tables_super_user_sees_allowed_set(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A super_user sees every allowed table; hidden ones are absent."""
    del seeded
    r = await client.get("/api/admin/db/tables", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {t["name"] for t in body["tables"]}

    # A handful of expected tables are surfaced.
    assert {"project", "scenario", "job", "tool", "user", "role"} <= names

    # Hidden tables stay hidden.
    assert "auth_session" not in names
    assert "alembic_version" not in names


@pytest.mark.asyncio
async def test_list_tables_marks_redacted_columns(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """``user.password_hash`` and ``aos_host.secret_ref`` are flagged as redacted."""
    del seeded
    r = await client.get("/api/admin/db/tables", headers=auth_headers)
    assert r.status_code == 200, r.text
    by_name = {t["name"]: t for t in r.json()["tables"]}

    assert "password_hash" in by_name["user"]["redacted_columns"]
    # The flagged column is also absent from the visible-columns list,
    # so the UI doesn't accidentally render an empty cell with the real
    # column name in the header.
    assert "password_hash" not in by_name["user"]["columns"]

    if "aos_host" in by_name:
        assert "secret_ref" in by_name["aos_host"]["redacted_columns"]
        assert "secret_ref" not in by_name["aos_host"]["columns"]


@pytest.mark.asyncio
async def test_tool_table_enriches_user_id_columns(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The seeded tool's ``installed_by_user_id`` renders as ``Full Name (username)``.

    Raw FK ints are friendlier to read once expanded — the explorer
    looks the user up and replaces the cell value in the response.
    Unresolved or NULL ids pass through unchanged.
    """
    del seeded
    r = await client.get("/api/admin/db/tables/tool", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"], "expected at least the seeded tool"
    row = body["rows"][0]
    installed = row.get("installed_by_user_id")
    # Fixture's super-user is the installer — full_name is "Root User",
    # username is "root". The enrichment should produce "Root User (root)".
    assert isinstance(installed, str)
    assert "(root)" in installed
    # archived_by_user_id is NULL on the freshly-installed tool — should
    # pass through untouched.
    assert row.get("archived_by_user_id") is None


@pytest.mark.asyncio
async def test_read_user_table_redacts_password_hash(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The ``user`` table page substitutes the placeholder for password_hash."""
    del seeded
    r = await client.get("/api/admin/db/tables/user", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"], "expected at least the seeded user"
    for row in body["rows"]:
        # Plaintext NEVER leaves the DB — every row's password_hash is
        # the opaque placeholder.
        assert row["password_hash"] == REDACTION_PLACEHOLDER


@pytest.mark.asyncio
async def test_read_hidden_table_returns_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A hidden table (live session tokens) is reported as not found."""
    del seeded
    r = await client.get("/api/admin/db/tables/auth_session", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_sort_by_unknown_column_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Sorting on a column that doesn't exist surfaces a friendly 400."""
    del seeded
    r = await client.get(
        "/api/admin/db/tables/user?sort_by=this_column_does_not_exist",
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "sort" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_sort_by_redacted_column_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Even the placeholder shape rejects sorts on redacted columns."""
    del seeded
    r = await client.get(
        "/api/admin/db/tables/user?sort_by=password_hash",
        headers=auth_headers,
    )
    assert r.status_code == 400


def _text_leaf(column: str, text: str) -> dict[str, Any]:
    """A free-text (substring) leaf condition for a filter tree."""
    return {"column": column, "match": "text", "text": text}


def _values_leaf(column: str, values: list[str], *, include_null: bool = False) -> dict[str, Any]:
    """A values (IN) leaf condition for a filter tree."""
    return {"column": column, "match": "values", "values": values, "include_null": include_null}


def _group(op: str, *children: dict[str, Any]) -> dict[str, Any]:
    """An and/or group node wrapping ``children``."""
    return {"op": op, "children": list(children)}


@pytest.mark.asyncio
async def test_filter_unknown_column_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A leaf naming a column that doesn't exist returns 400."""
    del seeded
    filters = json.dumps(_group("and", _text_leaf("this_does_not_exist", "x")))
    r = await client.get(f"/api/admin/db/tables/user?filters={filters}", headers=auth_headers)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_filter_substring_matches_case_insensitive(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A case-insensitive substring (text) leaf narrows the result set."""
    user_name = seeded["username"]
    # Uppercase a slice of the seeded username; the filter should still
    # match because the text leaf is LOWER(col) LIKE LOWER(value).
    needle = user_name[:3].upper()
    filters = json.dumps(_group("and", _text_leaf("username", needle)))
    r = await client.get(f"/api/admin/db/tables/user?filters={filters}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert any(row["username"] == user_name for row in body["rows"])


async def _role_names(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> set[str]:
    """All role names currently in the seeded ``role`` table."""
    r = await client.get("/api/admin/db/tables/role?page_size=1000", headers=auth_headers)
    assert r.status_code == 200, r.text
    return {row["name"] for row in r.json()["rows"]}


@pytest.mark.asyncio
async def test_filter_values_leaf_exact_match(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A values leaf does an exact IN-match, OR-combining the picked values."""
    del seeded
    names = await _role_names(client, auth_headers)
    pick = sorted(names)[:2]
    assert len(pick) == 2, "fixture should seed at least two roles"

    filters = json.dumps(_group("and", _values_leaf("name", pick)))
    r = await client.get(f"/api/admin/db/tables/role?filters={filters}", headers=auth_headers)
    assert r.status_code == 200, r.text
    got = {row["name"] for row in r.json()["rows"]}
    assert got == set(pick)


@pytest.mark.asyncio
async def test_filter_or_between_leaves(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """An OR group returns the union of its leaves' matches."""
    del seeded
    names = sorted(await _role_names(client, auth_headers))
    a, b = names[0], names[1]

    filters = json.dumps(_group("or", _values_leaf("name", [a]), _values_leaf("name", [b])))
    r = await client.get(f"/api/admin/db/tables/role?filters={filters}", headers=auth_headers)
    assert r.status_code == 200, r.text
    got = {row["name"] for row in r.json()["rows"]}
    assert got == {a, b}


@pytest.mark.asyncio
async def test_filter_nested_and_within_or(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Nesting works: (name=A) OR (name=B AND name~substr-of-B)."""
    del seeded
    names = sorted(await _role_names(client, auth_headers))
    a, b = names[0], names[1]

    tree = _group(
        "or",
        _values_leaf("name", [a]),
        _group("and", _values_leaf("name", [b]), _text_leaf("name", b[:3])),
    )
    r = await client.get(
        f"/api/admin/db/tables/role?filters={json.dumps(tree)}", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    got = {row["name"] for row in r.json()["rows"]}
    assert got == {a, b}


@pytest.mark.asyncio
async def test_filter_include_null_matches_null_column(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """include_null on a values leaf matches rows where the column IS NULL.

    The freshly-installed seeded tool has a NULL ``archived_by_user_id``,
    so an include-null filter must surface it.
    """
    del seeded
    tree = _group("and", _values_leaf("archived_by_user_id", [], include_null=True))
    r = await client.get(
        f"/api/admin/db/tables/tool?filters={json.dumps(tree)}", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"], "expected the seeded tool (NULL archived_by_user_id)"
    assert all(row["archived_by_user_id"] is None for row in body["rows"])


@pytest.mark.asyncio
async def test_filter_bad_op_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A group with an op other than and/or is rejected."""
    del seeded
    filters = json.dumps({"op": "xor", "children": [_text_leaf("name", "x")]})
    r = await client.get(f"/api/admin/db/tables/role?filters={filters}", headers=auth_headers)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_filter_bad_match_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A leaf with an unknown match mode is rejected."""
    del seeded
    filters = json.dumps(_group("and", {"column": "name", "match": "bogus", "text": "x"}))
    r = await client.get(f"/api/admin/db/tables/role?filters={filters}", headers=auth_headers)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_filter_too_deeply_nested_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A tree nested past the depth cap is rejected rather than recursing."""
    del seeded
    node: dict[str, Any] = _text_leaf("name", "x")
    for _ in range(40):
        node = _group("and", node)
    r = await client.get(
        f"/api/admin/db/tables/role?filters={json.dumps(node)}", headers=auth_headers
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_filter_empty_group_is_noop(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """An empty group adds no constraint — same total as no filter at all."""
    del seeded
    unfiltered = await client.get("/api/admin/db/tables/role", headers=auth_headers)
    assert unfiltered.status_code == 200
    total = unfiltered.json()["total_rows"]

    filters = json.dumps(_group("and"))
    r = await client.get(f"/api/admin/db/tables/role?filters={filters}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["total_rows"] == total


@pytest.mark.asyncio
async def test_pagination_respects_page_and_size(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """``page_size`` caps the row count; ``total_rows`` reflects the unpaged total."""
    del seeded
    r = await client.get("/api/admin/db/tables/role?page=1&page_size=1", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) <= 1
    assert body["page"] == 1
    assert body["page_size"] == 1
    # 4 built-in roles seeded — total_rows must be at least that.
    expected_min = 4
    assert body["total_rows"] >= expected_min


@pytest.mark.asyncio
async def test_filter_must_be_json_object(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A non-JSON ``filters`` value is rejected as a 400 with a clear message."""
    del seeded
    r = await client.get("/api/admin/db/tables/user?filters=not-json", headers=auth_headers)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_explore_permission_gate(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A regular user — without ``database.explore`` — is refused 403."""
    # Create a regular user via the admin endpoint (super_user can).
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "no_explore_user",
            "email": "no_explore@matofx.test",
            "password": "ASecureP@ssw0rd!",
            "full_name": "No Explore",
            "role": "regular_user",
        },
    )
    assert create.status_code in (200, 201), create.text

    # Log in as that user and try to hit the explorer.
    del seeded
    login = await client.post(
        "/api/auth/login",
        json={"username": "no_explore_user", "password": "ASecureP@ssw0rd!"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get("/api/admin/db/tables", headers=headers)
    assert r.status_code == 403
    assert "database.explore" in r.json()["detail"]


# ---------- column distinct values -------------------------------------------


@pytest.mark.asyncio
async def test_distinct_values_under_threshold(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A low-cardinality column returns its distinct values, not ``too_many``."""
    del seeded
    r = await client.get("/api/admin/db/tables/role/columns/name/distinct", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["too_many"] is False
    assert body["has_null"] is False
    names = await _role_names(client, auth_headers)
    assert set(body["values"]) == names


@pytest.mark.asyncio
async def test_distinct_values_over_threshold(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the distinct-value limit, the picker is suppressed (``too_many``)."""
    del seeded
    # Squeeze the limit below the seeded role count so the over-cap path
    # fires without seeding dozens of rows.
    monkeypatch.setattr("matofx.db.explorer.DISTINCT_VALUE_LIMIT", 1)
    r = await client.get("/api/admin/db/tables/role/columns/name/distinct", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["too_many"] is True
    assert body["values"] == []


@pytest.mark.asyncio
async def test_distinct_values_redacted_column_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A redacted column can't have its distinct values listed."""
    del seeded
    r = await client.get(
        "/api/admin/db/tables/user/columns/password_hash/distinct", headers=auth_headers
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_distinct_values_unknown_column_returns_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """An unknown column returns 400."""
    del seeded
    r = await client.get(
        "/api/admin/db/tables/user/columns/nope_not_here/distinct", headers=auth_headers
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_distinct_values_hidden_table_returns_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A hidden table is 404 here too."""
    del seeded
    r = await client.get(
        "/api/admin/db/tables/auth_session/columns/id/distinct", headers=auth_headers
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_distinct_values_requires_permission(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A regular user without ``database.explore`` is refused 403."""
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "no_distinct_user",
            "email": "no_distinct@matofx.test",
            "password": "ASecureP@ssw0rd!",
            "full_name": "No Distinct",
            "role": "regular_user",
        },
    )
    assert create.status_code in (200, 201), create.text

    del seeded
    login = await client.post(
        "/api/auth/login",
        json={"username": "no_distinct_user", "password": "ASecureP@ssw0rd!"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get("/api/admin/db/tables/role/columns/name/distinct", headers=headers)
    assert r.status_code == 403


# ---------- per-user activity ------------------------------------------------


@pytest.mark.asyncio
async def test_user_activity_requires_auth(client: httpx.AsyncClient) -> None:
    """No bearer → 401 (sticking to the same gate the rest of the explorer uses)."""
    r = await client.get("/api/admin/users/1/activity")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_user_activity_returns_subject_for_seeded_user(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Picking the seeded user surfaces the right subject info + empty defaults."""
    user_id = int(seeded["user_id"])
    r = await client.get(f"/api/admin/users/{user_id}/activity", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["subject"]["id"] == user_id
    assert body["subject"]["username"] == seeded["username"]
    assert body["subject"]["role"] == "super_user"

    # Top-level shape — every section present, even when empty.
    assert isinstance(body["projects"], list)
    assert isinstance(body["scenarios"], list)
    assert isinstance(body["jobs"], list)
    assert isinstance(body["workflows"], list)
    assert isinstance(body["parametric_studies"], list)
    assert isinstance(body["optimisations"], list)
    assert isinstance(body["surrogate_models"], list)
    assert isinstance(body["cohorts"], list)
    assert isinstance(body["audit_events"], list)
    assert isinstance(body["logon_events"], list)


@pytest.mark.asyncio
async def test_user_activity_includes_owned_projects(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A project owned by the user shows up in the projects section."""
    user_id = int(seeded["user_id"])
    create = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "owned by tester", "description": "just for the test"},
    )
    assert create.status_code == 201, create.text

    r = await client.get(f"/api/admin/users/{user_id}/activity", headers=auth_headers)
    assert r.status_code == 200
    names = {p["name"] for p in r.json()["projects"]}
    assert "owned by tester" in names


@pytest.mark.asyncio
async def test_user_activity_includes_scenarios_user_created(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A scenario the user created surfaces under their activity.

    Scenario creation isn't recorded on the row itself, so the
    endpoint resolves it via the audit log
    (``hierarchy.scenario.create`` rows where actor matches the
    user). This proves that path lights up end-to-end.
    """
    user_id = int(seeded["user_id"])
    project = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "scen-test-project", "description": ""},
    )
    assert project.status_code == 201, project.text
    project_id = int(project.json()["id"])
    scenario = await client.post(
        f"/api/projects/{project_id}/scenarios",
        headers=auth_headers,
        json={"name": "first scenario", "description": ""},
    )
    assert scenario.status_code == 201, scenario.text

    r = await client.get(f"/api/admin/users/{user_id}/activity", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    names = {s["name"] for s in body["scenarios"]}
    assert "first scenario" in names
    # Project name resolves through the join.
    by_name = {s["name"]: s for s in body["scenarios"]}
    assert by_name["first scenario"]["project_name"] == "scen-test-project"


@pytest.mark.asyncio
async def test_user_activity_includes_owned_workflow(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """A custom workflow the user owns surfaces under their activity.

    Seeds the row straight through the ORM (the public create flow needs
    a fuller fixture); the section is built from ``owner_id`` /
    ``installed_by_user_id`` and the per-user run tally, so an owned row
    with no runs still appears with ``owned=True`` and ``run_count=0``.
    """
    from matofx.workflows.orm import Workflow  # noqa: PLC0415

    user_id = int(seeded["user_id"])
    async with session_scope(api_factory) as session:
        session.add(
            Workflow(
                alias=f"custom_{user_id}_my_pipeline",
                name="My Pipeline",
                version="1.0.0",
                kind="custom",
                state="released",
                descriptor={},
                installed_by_user_id=user_id,
                owner_id=user_id,
                visibility="private",
            )
        )

    r = await client.get(f"/api/admin/users/{user_id}/activity", headers=auth_headers)
    assert r.status_code == 200, r.text
    workflows = r.json()["workflows"]
    by_name = {w["name"]: w for w in workflows}
    assert "My Pipeline" in by_name
    row = by_name["My Pipeline"]
    assert row["kind"] == "custom"
    assert row["owned"] is True
    assert row["run_count"] == 0
    assert row["last_run_at"] is None


@pytest.mark.asyncio
async def test_user_activity_includes_owned_cohort(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """A job cohort the user created surfaces under their activity.

    Exercises the shared ``_owned_named_records`` path that also powers
    the parametric-studies, optimisations and surrogate-models sections,
    so one functional case covers all four name/state/created lists.
    """
    from matofx.studies.cohort.orm import JobCohort  # noqa: PLC0415

    user_id = int(seeded["user_id"])
    tool_alias = str(seeded["tool_alias"])
    async with session_scope(api_factory) as session:
        session.add(
            JobCohort(
                name="My Cohort",
                description="",
                tool_alias=tool_alias,
                tolerated_tool_version_ids=[1],
                auto_grow=True,
                state="active",
                created_by_user_id=user_id,
            )
        )

    r = await client.get(f"/api/admin/users/{user_id}/activity", headers=auth_headers)
    assert r.status_code == 200, r.text
    cohorts = r.json()["cohorts"]
    by_name = {c["name"]: c for c in cohorts}
    assert "My Cohort" in by_name
    assert by_name["My Cohort"]["state"] == "active"


@pytest.mark.asyncio
async def test_user_activity_unknown_user_returns_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Asking about a user that doesn't exist returns a friendly 404."""
    del seeded
    r = await client.get("/api/admin/users/9999999/activity", headers=auth_headers)
    assert r.status_code == 404
    assert "9999999" in r.json()["detail"]


@pytest.mark.asyncio
async def test_user_activity_permission_gate(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A regular user without ``database.explore`` is refused 403 here too."""
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "no_activity_user",
            "email": "no_activity@matofx.test",
            "password": "ASecureP@ssw0rd!",
            "full_name": "No Activity",
            "role": "regular_user",
        },
    )
    assert create.status_code in (200, 201), create.text

    login = await client.post(
        "/api/auth/login",
        json={"username": "no_activity_user", "password": "ASecureP@ssw0rd!"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    user_id = int(seeded["user_id"])
    r = await client.get(f"/api/admin/users/{user_id}/activity", headers=headers)
    assert r.status_code == 403
    assert "database.explore" in r.json()["detail"]


# ---------- diagnostics tab -------------------------------------------------


async def _make_scenario(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> int:
    """Helper: stand up a fresh project + scenario for jobs to attach to.

    Project names are unique per owner (``uq_project_owner_name``), and
    a scenario admits only one job per ``tool_alias``
    (``uq_job_scenario_tool`` from migration 0017). So every call mints
    a uniquely-named project + scenario — callers that need several
    jobs with the same tool just call this once per job.
    """
    tag = uuid.uuid4().hex[:8]
    p = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": f"diag-project-{tag}", "description": ""},
    )
    assert p.status_code == 201, p.text
    project_id = p.json()["id"]
    s = await client.post(
        f"/api/projects/{project_id}/scenarios",
        headers=auth_headers,
        json={"name": f"diag-scenario-{tag}", "description": ""},
    )
    assert s.status_code == 201, s.text
    return int(s.json()["id"])


async def _seed_job_new_scenario(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    *,
    user_id: int,
    tool_alias: str,
    state: str,
    name: str,
    updated_at: _dt.datetime | None = None,
) -> int:
    """Seed one job in its own fresh scenario.

    The diagnostics endpoint counts jobs DB-wide, not per-scenario, so
    isolating each seeded job in its own scenario keeps the global
    counts identical while satisfying the one-tool-per-scenario
    uniqueness constraint.
    """
    scenario_id = await _make_scenario(client, auth_headers)
    return await _seed_job_in_state(
        api_factory,
        scenario_id=scenario_id,
        user_id=user_id,
        tool_alias=tool_alias,
        state=state,
        name=name,
        updated_at=updated_at,
    )


async def _seed_job_in_state(
    api_factory: SessionFactory,
    *,
    scenario_id: int,
    user_id: int,
    tool_alias: str,
    state: str,
    name: str,
    updated_at: _dt.datetime | None = None,
) -> int:
    """Seed a Job row directly via the ORM.

    Per the test-seeding feedback memory we go through the ORM rather
    than raw SQL — SQLite's lexicographic datetime comparison breaks
    the CHECK constraints when the timestamp formats don't match the
    ORM's writes.

    All timestamps are pinned explicitly so the CHECK chain
    (``created_at <= started_at <= completed_at``) is satisfied —
    relying on the ORM's ``default=_utcnow`` for ``created_at`` would
    race against the explicit ``started_at`` we set here.

    Job.tool_version_id is NOT NULL since Session B; we resolve the
    latest snapshot for the alias on every seed.
    """
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    async with session_scope(api_factory) as session:
        when = updated_at if updated_at is not None else _dt.datetime.now(tz=_dt.UTC)
        started = when if state in ("running", "complete", "failed") else None
        completed = when if state in ("complete", "failed") else None
        latest_version_id = (
            await session.execute(
                sa.select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias)
                .order_by(ToolVersion.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        job = Job(
            scenario_id=scenario_id,
            name=name,
            tool_alias=tool_alias,
            tool_version_id=int(latest_version_id),
            state=state,
            inputs={},
            created_by_user_id=user_id,
            created_at=when,
            updated_at=when,
            started_at=started,
            completed_at=completed,
        )
        session.add(job)
        await session.flush()
        return int(job.id)


@pytest.mark.asyncio
async def test_diagnostics_requires_auth(client: httpx.AsyncClient) -> None:
    """No bearer → 401."""
    r = await client.get("/api/admin/db/diagnostics")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_diagnostics_permission_gate(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Regular user without ``database.explore`` → 403."""
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "no_diag_user",
            "email": "no_diag@matofx.test",
            "password": "ASecureP@ssw0rd!",
            "full_name": "No Diag",
            "role": "regular_user",
        },
    )
    assert create.status_code in (200, 201), create.text

    del seeded
    login = await client.post(
        "/api/auth/login",
        json={"username": "no_diag_user", "password": "ASecureP@ssw0rd!"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get("/api/admin/db/diagnostics", headers=headers)
    assert r.status_code == 403
    assert "database.explore" in r.json()["detail"]


@pytest.mark.asyncio
async def test_diagnostics_empty_db_defaults(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """No jobs yet → zero counts, empty examples, no orphans, queue=(0,0)."""
    del seeded
    r = await client.get("/api/admin/db/diagnostics", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["failed_jobs_7d"]["count"] == 0
    assert body["failed_jobs_7d"]["examples"] == []
    assert body["queue_depth"] == {"queued": 0, "running": 0}
    # Recent users may include the seeded super-user (audit activity
    # from bootstrap + tool install). We only insist on the shape.
    assert isinstance(body["recent_users"], list)
    assert body["orphaned_working_dirs"]["checked"] is True
    assert body["orphaned_working_dirs"]["items"] == []


@pytest.mark.asyncio
async def test_diagnostics_failed_jobs_in_window(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Failed jobs inside the 7-day window count; older ones don't."""
    user_id = int(seeded["user_id"])
    tool_alias = str(seeded["tool_alias"])

    now = _dt.datetime.now(tz=_dt.UTC)
    fresh_id = await _seed_job_new_scenario(
        client,
        auth_headers,
        api_factory,
        user_id=user_id,
        tool_alias=tool_alias,
        state="failed",
        name="fresh fail",
        updated_at=now - _dt.timedelta(hours=1),
    )
    # Older than 7 days — must drop out of the window.
    await _seed_job_new_scenario(
        client,
        auth_headers,
        api_factory,
        user_id=user_id,
        tool_alias=tool_alias,
        state="failed",
        name="ancient fail",
        updated_at=now - _dt.timedelta(days=14),
    )
    # A complete job within the window — must NOT be counted.
    await _seed_job_new_scenario(
        client,
        auth_headers,
        api_factory,
        user_id=user_id,
        tool_alias=tool_alias,
        state="complete",
        name="happy job",
        updated_at=now - _dt.timedelta(hours=2),
    )

    r = await client.get("/api/admin/db/diagnostics", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed_jobs_7d"]["count"] == 1
    examples = body["failed_jobs_7d"]["examples"]
    assert len(examples) == 1
    assert examples[0]["id"] == fresh_id
    assert examples[0]["name"] == "fresh fail"


@pytest.mark.asyncio
async def test_diagnostics_queue_depth_counts(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Queue depth reflects live counts of queued + running jobs."""
    user_id = int(seeded["user_id"])
    tool_alias = str(seeded["tool_alias"])

    for name in ("q1", "q2", "q3"):
        await _seed_job_new_scenario(
            client,
            auth_headers,
            api_factory,
            user_id=user_id,
            tool_alias=tool_alias,
            state="queued",
            name=name,
        )
    for name in ("r1", "r2"):
        await _seed_job_new_scenario(
            client,
            auth_headers,
            api_factory,
            user_id=user_id,
            tool_alias=tool_alias,
            state="running",
            name=name,
        )

    r = await client.get("/api/admin/db/diagnostics", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queue_depth"]["queued"] == 3
    assert body["queue_depth"]["running"] == 2


@pytest.mark.asyncio
async def test_diagnostics_recent_users_include_active_user(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The seeded user has audit activity (user-create, tool install) → appears."""
    user_id = int(seeded["user_id"])
    # Drive a fresh audit row to make sure we're definitely the
    # newest activity in the table.
    p = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "drive-audit", "description": ""},
    )
    assert p.status_code == 201, p.text

    r = await client.get("/api/admin/db/diagnostics", headers=auth_headers)
    assert r.status_code == 200, r.text
    ids = {int(u["user_id"]) for u in r.json()["recent_users"]}
    assert user_id in ids


@pytest.mark.asyncio
async def test_diagnostics_orphaned_working_dirs_detected(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A ``jobs/job_NNNNNNN`` dir whose id has no row surfaces as an orphan."""
    mount: Path = seeded["mount_path"]
    orphan_dir = mount / "jobs" / "job_9999999"
    orphan_dir.mkdir(parents=True)

    r = await client.get("/api/admin/db/diagnostics", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    orphans = body["orphaned_working_dirs"]
    assert orphans["checked"] is True
    paths = {item["relative_path"] for item in orphans["items"]}
    assert "jobs/job_9999999" in paths
    locs = {item["storage_location_name"] for item in orphans["items"]}
    assert "local" in locs


@pytest.mark.asyncio
async def test_diagnostics_orphaned_working_dirs_ignores_live_job(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """A dir whose id matches a live job row is NOT flagged as an orphan."""
    scenario_id = await _make_scenario(client, auth_headers)
    user_id = int(seeded["user_id"])
    tool_alias = str(seeded["tool_alias"])
    job_id = await _seed_job_in_state(
        api_factory,
        scenario_id=scenario_id,
        user_id=user_id,
        tool_alias=tool_alias,
        state="created",
        name="live job",
    )

    mount: Path = seeded["mount_path"]
    live_dir = mount / "jobs" / f"job_{job_id:07d}"
    live_dir.mkdir(parents=True)

    r = await client.get("/api/admin/db/diagnostics", headers=auth_headers)
    assert r.status_code == 200, r.text
    paths = {item["relative_path"] for item in r.json()["orphaned_working_dirs"]["items"]}
    assert f"jobs/job_{job_id:07d}" not in paths
