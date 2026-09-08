"""API tests for the project metadata taxonomy router (ADR-0002)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest


@pytest.mark.asyncio
async def test_metadata_field_crud_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded  # `auth_headers` is a super_user → holds projects.manage_metadata.

    # Empty taxonomy initially.
    r = await client.get("/api/metadata/fields", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []

    # Create a select field.
    created = await client.post(
        "/api/admin/metadata/fields",
        headers=auth_headers,
        json={
            "key": "programme",
            "label": "Programme",
            "field_type": "single_select",
            "required": True,
            "show_in_table": True,
        },
    )
    assert created.status_code == 201, created.text
    fid = created.json()["id"]

    # Add a value.
    val = await client.post(
        f"/api/admin/metadata/fields/{fid}/values",
        headers=auth_headers,
        json={"value": "Alpha"},
    )
    assert val.status_code == 201, val.text
    vid = val.json()["id"]

    # Active list now surfaces field + value.
    active = await client.get("/api/metadata/fields", headers=auth_headers)
    body = active.json()
    assert len(body) == 1
    assert body[0]["key"] == "programme"
    assert body[0]["values"][0]["id"] == vid

    # Retire the field → gone from the active list, present in admin list.
    patched = await client.patch(
        f"/api/admin/metadata/fields/{fid}",
        headers=auth_headers,
        json={"active": False},
    )
    assert patched.status_code == 200
    after_retire = await client.get("/api/metadata/fields", headers=auth_headers)
    assert after_retire.json() == []
    admin_list = await client.get("/api/admin/metadata/fields", headers=auth_headers)
    assert len(admin_list.json()) == 1
    assert admin_list.json()[0]["active"] is False


@pytest.mark.asyncio
async def test_invalid_field_type_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    r = await client.post(
        "/api/admin/metadata/fields",
        headers=auth_headers,
        json={"key": "x", "label": "X", "field_type": "freetext"},
    )
    # Pydantic pattern rejects unknown type → 422 (or 400 from the
    # service if it slips past). Either way it's a client error.
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_pattern_field_create_carries_format(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    created = await client.post(
        "/api/admin/metadata/fields",
        headers=auth_headers,
        json={
            "key": "part",
            "label": "Part number",
            "field_type": "number",
            "number_format": "pattern",
            "format_mask": "AA-000-0000",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["number_format"] == "pattern"
    assert body["format_mask"] == "AA-000-0000"
    # Surfaces on the read-only project form taxonomy too.
    active = await client.get("/api/metadata/fields", headers=auth_headers)
    assert active.json()[0]["format_mask"] == "AA-000-0000"


@pytest.mark.asyncio
async def test_value_reorder_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    created = await client.post(
        "/api/admin/metadata/fields",
        headers=auth_headers,
        json={"key": "disc", "label": "Discipline", "field_type": "single_select"},
    )
    fid = created.json()["id"]
    ids: list[int] = []
    for label in ("A", "B", "C"):
        r = await client.post(
            f"/api/admin/metadata/fields/{fid}/values",
            headers=auth_headers,
            json={"value": label},
        )
        ids.append(r.json()["id"])
    reorder = await client.patch(
        f"/api/admin/metadata/fields/{fid}/values/order",
        headers=auth_headers,
        json={"value_ids": [ids[2], ids[0], ids[1]]},
    )
    assert reorder.status_code == 204, reorder.text
    active = await client.get("/api/metadata/fields", headers=auth_headers)
    values = active.json()[0]["values"]
    assert [v["value"] for v in values] == ["C", "A", "B"]


@pytest.mark.asyncio
async def test_value_on_number_field_rejected(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    created = await client.post(
        "/api/admin/metadata/fields",
        headers=auth_headers,
        json={"key": "budget", "label": "Budget", "field_type": "number"},
    )
    fid = created.json()["id"]
    bad = await client.post(
        f"/api/admin/metadata/fields/{fid}/values",
        headers=auth_headers,
        json={"value": "nope"},
    )
    assert bad.status_code == 400
