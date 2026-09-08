"""API tests for tool guide images (static input-entry help pictures).

Exercises the browser-facing flow: install a tool, POST a guide-image
set into its bundle, serve them back, then reorder / drop / clear — all
through the admin + tools routers directly.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

import httpx
import pytest


def _descriptor_json(alias: str) -> bytes:
    payload = {
        "schema_version": 1,
        "alias": alias,
        "name": f"{alias} test",
        "version": "1.0",
        "description": "Guide-image test tool.",
        "main_script": "main.py",
        "inputs": [],
        "outputs": [],
        "execution_backend": {"kind": "subprocess"},
    }
    return json.dumps(payload).encode("utf-8")


def _static_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", b"# placeholder\n")
    return buf.getvalue()


async def _install(client: httpx.AsyncClient, headers: dict[str, str], alias: str) -> None:
    metadata = {
        "alias": alias,
        "name": f"{alias} test",
        "version": "1.0",
        "short_description": "Guide-image tool.",
        "long_description": None,
        "main_script": "main.py",
        "teams": [],
        "downstream_tools": [],
        "cpu_cores": 1,
        "ram_mb": 256,
        "gpu_memory_mb": 0,
        "licences_required": [],
        "duration_class": "snappy",
        "approvals_policy": "none",
        "approver_usernames": [],
        "email_notifications": True,
    }
    resp = await client.post(
        "/api/admin/tools",
        headers=headers,
        data={"metadata": json.dumps(metadata)},
        files=[
            (
                "descriptor_json",
                (f"{alias}_descriptors.json", _descriptor_json(alias), "application/json"),
            ),
            ("static_zip", ("bundle.zip", _static_zip(), "application/zip")),
        ],
    )
    assert resp.status_code == 201, resp.text


def _cas_name(data: bytes, ext: str) -> str:
    return f"guide-{hashlib.sha1(data).hexdigest()[:16]}.{ext}"  # noqa: S324 - CAS name, not security


async def _guides(
    client: httpx.AsyncClient, headers: dict[str, str], alias: str
) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/tools/{alias}", headers=headers)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["descriptor"].get("input_guides") or [])


@pytest.mark.asyncio
async def test_guide_images_add_serve_reorder_clear(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    alias = "guide_tool"
    await _install(client, auth_headers, alias)

    img_a, img_b = b"\x89PNG-fake-a", b"\xff\xd8-fake-b"
    # Add two images with captions.
    add = await client.post(
        f"/api/admin/tools/{alias}/guide-images",
        headers=auth_headers,
        data={"manifest": json.dumps([{"caption": "Diagram A"}, {"caption": "Diagram B"}])},
        files=[
            ("images", ("a.png", img_a, "image/png")),
            ("images", ("b.jpg", img_b, "image/jpeg")),
        ],
    )
    assert add.status_code == 200, add.text

    guides = await _guides(client, auth_headers, alias)
    assert [g["caption"] for g in guides] == ["Diagram A", "Diagram B"]
    assert guides[0]["filename"] == _cas_name(img_a, "png")
    assert guides[1]["filename"] == _cas_name(img_b, "jpg")

    # Serve both back; index past the end is 404.
    r0 = await client.get(f"/api/tools/{alias}/guide-images/0", headers=auth_headers)
    assert r0.status_code == 200
    assert r0.content == img_a
    assert r0.headers["content-type"].startswith("image/png")
    r1 = await client.get(f"/api/tools/{alias}/guide-images/1", headers=auth_headers)
    assert r1.status_code == 200
    assert r1.content == img_b
    assert r1.headers["content-type"].startswith("image/jpeg")
    r2 = await client.get(f"/api/tools/{alias}/guide-images/2", headers=auth_headers)
    assert r2.status_code == 404

    # Reorder to [B] only (drop A), keeping B by its existing filename, new caption.
    reorder = await client.post(
        f"/api/admin/tools/{alias}/guide-images",
        headers=auth_headers,
        data={
            "manifest": json.dumps(
                [{"existing_filename": _cas_name(img_b, "jpg"), "caption": "Only B now"}]
            )
        },
    )
    assert reorder.status_code == 200, reorder.text
    guides = await _guides(client, auth_headers, alias)
    assert [g["caption"] for g in guides] == ["Only B now"]
    # A's bundle file is gone -> index 0 now serves B.
    kept = await client.get(f"/api/tools/{alias}/guide-images/0", headers=auth_headers)
    assert kept.status_code == 200
    assert kept.content == img_b

    # Referencing an image that isn't a current guide is rejected.
    bad = await client.post(
        f"/api/admin/tools/{alias}/guide-images",
        headers=auth_headers,
        data={"manifest": json.dumps([{"existing_filename": "guide-deadbeef.png"}])},
    )
    assert bad.status_code == 400

    # Clear all.
    clear = await client.post(
        f"/api/admin/tools/{alias}/guide-images",
        headers=auth_headers,
        data={"manifest": json.dumps([])},
    )
    assert clear.status_code == 200, clear.text
    assert await _guides(client, auth_headers, alias) == []
    gone = await client.get(f"/api/tools/{alias}/guide-images/0", headers=auth_headers)
    assert gone.status_code == 404
