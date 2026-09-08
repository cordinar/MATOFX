"""Admin-router tests for the multipart tool create / edit / replace flow.

The seeded fixture installs ``material_properties`` from disk via the
:func:`install_from_path` loader; these tests exercise the
*browser-uploaded* paths (multipart with descriptor JSON + static
ZIP) against the admin API directly.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest


def _make_descriptor_json(alias: str, *, version: str = "1.0") -> bytes:
    """Build a minimal v2-shape descriptor JSON for upload tests."""
    payload = {
        "schema_version": 1,
        "alias": alias,
        "name": f"{alias} test",
        "version": version,
        "description": "Test tool installed via the multipart endpoint.",
        "main_script": "main.py",
        "teams": [],
        "downstream_tools": [],
        "inputs": [],
        "outputs": [],
        "input_groups": [],
        "output_groups": [],
        "input_column_layout": {},
        "output_column_layout": {},
        "retain_files": [],
        "delete_io_files": [],
        "resources": {"cpu_cores": 1, "ram_mb": 256, "gpu_memory_mb": 0},
        "licences_required": [],
        "duration_class": "snappy",
        "approvals_policy": "none",
        "approver_usernames": [],
        "email_notifications": True,
        "execution_backend": {"kind": "subprocess"},
    }
    return json.dumps(payload).encode("utf-8")


def _make_static_zip() -> bytes:
    """Tiny in-memory ZIP containing a stub ``main.py``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", b"# placeholder\n")
    return buf.getvalue()


def _metadata(alias: str, *, version: str = "1.0", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alias": alias,
        "name": f"{alias} test",
        "version": version,
        "short_description": "Uploaded via multipart in tests.",
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
    base.update(overrides)
    return base


def _files(
    *,
    alias: str,
    descriptor_bytes: bytes | None = None,
    zip_bytes: bytes | None = None,
) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        (
            "descriptor_json",
            (
                f"{alias}_descriptors.json",
                descriptor_bytes if descriptor_bytes is not None else _make_descriptor_json(alias),
                "application/json",
            ),
        ),
        (
            "static_zip",
            (
                "bundle.zip",
                zip_bytes if zip_bytes is not None else _make_static_zip(),
                "application/zip",
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_install_lands_at_initial_version_with_change_log_entry(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """New tools always start at 0.1.0 with one change-log entry."""
    del seeded
    alias = "version_baseline_tool"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias, version="9.9.9"))},
        files=_files(alias=alias, descriptor_bytes=_make_descriptor_json(alias, version="9.9.9")),
    )
    assert install.status_code == 201, install.text
    # Operator's "9.9.9" is ignored — server forces 0.1.0.
    assert install.json()["version"] == "0.1.0"
    bundle_dir = Path(install.json()["bundle_path"])
    assert bundle_dir.name == "0.1.0"

    versions = await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    assert versions.status_code == 200
    entries = versions.json()["versions"]
    assert len(entries) == 1
    assert entries[0]["version"] == "0.1.0"
    assert "Initial install" in entries[0]["change_summary"]
    assert "(root)" in entries[0]["created_by_label"]


@pytest.mark.asyncio
async def test_metadata_edit_patch_bumps_and_logs_field_diffs(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Update-descriptor patch-bumps + records old/new in changes JSON."""
    del seeded
    alias = "patch_bump_tool"
    await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )

    update = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json={
            "name": "Renamed Tool",
            "version": "ignored",
            "short_description": "Edited description",
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
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["version"] == "0.1.1"

    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    # Newest first → patch entry is index 0, install entry is index 1.
    assert len(versions) == 2
    patch_entry = versions[0]
    assert patch_entry["version"] == "0.1.1"
    assert "patch bump" in patch_entry["change_summary"].lower()
    changes = patch_entry["changes"]
    assert "name" in changes
    assert changes["name"]["old"].endswith(" test")
    assert changes["name"]["new"] == "Renamed Tool"
    assert "short_description" in changes


@pytest.mark.asyncio
async def test_metadata_no_op_edit_does_not_create_snapshot(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Submitting an unchanged descriptor returns the existing row + no snapshot."""
    del seeded
    alias = "noop_tool"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    body_after_install = _metadata(alias)
    # Build the update body to match the descriptor exactly — no
    # metadata field changes.
    no_op_body = {k: v for k, v in body_after_install.items() if k not in {"alias", "main_script"}}
    update = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json=no_op_body,
    )
    assert update.status_code == 200
    # Version stays at 0.1.0 (no patch bump for a no-op).
    assert update.json()["version"] == "0.1.0"
    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    assert len(versions) == 1  # only the install snapshot
    del install


@pytest.mark.asyncio
async def test_first_release_promotion_snaps_to_one_zero_zero(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Promoting from 0.x.y to released anchors the version at 1.0.0."""
    del seeded
    alias = "snap_tool"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.json()["version"] == "0.1.0"

    # Walk new → test → released. Only the released hop snaps.
    promote_test = await client.post(
        f"/api/admin/tools/{alias}/promote",
        headers=auth_headers,
        json={"new_state": "test"},
    )
    assert promote_test.status_code == 200
    assert promote_test.json()["version"] == "0.1.0"  # no snap on test promotion

    promote_released = await client.post(
        f"/api/admin/tools/{alias}/promote",
        headers=auth_headers,
        json={"new_state": "released"},
    )
    assert promote_released.status_code == 200
    assert promote_released.json()["version"] == "1.0.0"

    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    snap_entry = versions[0]
    assert snap_entry["version"] == "1.0.0"
    assert "Promoted to Released" in snap_entry["change_summary"]


@pytest.mark.asyncio
async def test_create_tool_via_multipart(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Happy path: form metadata + descriptor JSON + static ZIP install a fresh tool."""
    del seeded
    alias = "uploaded_tool"
    response = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["alias"] == alias
    assert body["state"] == "new"
    assert body["bundle_path"]
    # Bundle should now exist on the staged path with the descriptor file.
    bundle_dir = Path(body["bundle_path"])
    assert bundle_dir.is_dir()
    assert (bundle_dir / "descriptor.json").is_file()
    assert (bundle_dir / "main.py").is_file()


@pytest.mark.asyncio
async def test_create_tool_rejects_duplicate_alias(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Re-installing a tool with an alias already on disk returns 409."""
    del seeded
    alias = "dup_alias_tool"
    first = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_create_tool_rejects_bad_zip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A non-ZIP payload in the static-files slot returns 409 with a friendly message."""
    del seeded
    alias = "bad_zip_tool"
    response = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias, zip_bytes=b"not a zip file"),
    )
    assert response.status_code == 409
    assert "ZIP" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_admin_tool_returns_full_descriptor(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The detail endpoint returns the descriptor dict the edit page needs."""
    alias = str(seeded["tool_alias"])
    response = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["alias"] == alias
    assert body["descriptor"]
    assert isinstance(body["descriptor"], dict)
    assert body["descriptor"]["alias"] == alias


@pytest.mark.asyncio
async def test_update_tool_descriptor_metadata_only(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Editing metadata via update-descriptor leaves the bundle path alone."""
    alias = str(seeded["tool_alias"])
    before = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    bundle_path_before = before.json()["bundle_path"]

    update = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json={
            "name": "Renamed in tests",
            "version": "1.0.1",  # ignored — Session B's auto-bump owns the version
            "short_description": "Edited via update-descriptor.",
            "teams": [],
            "downstream_tools": [],
            "cpu_cores": 2,
            "ram_mb": 512,
            "gpu_memory_mb": 0,
            "licences_required": [],
            "duration_class": "medium",
            "approvals_policy": "none",
            "approver_usernames": [],
            "email_notifications": True,
        },
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["name"] == "Renamed in tests"
    # The seeded tool walks new → test → released during fixture
    # setup, which snaps the version to 1.0.0 (first-release rule).
    # A metadata edit then patch-bumps to 1.0.1. The "1.0.1" in the
    # request body itself is ignored — server is the version authority.
    assert body["version"] == "1.0.1"

    after = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    assert after.json()["bundle_path"] == bundle_path_before


@pytest.mark.asyncio
async def test_replace_tool_bundle_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Replace-bundle stages the new bundle in a per-version dir + major-bumps."""
    del seeded
    # Install a fresh tool so the test owns the bundle path.
    alias = "replace_tool"
    initial = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert initial.status_code == 201
    initial_bundle = Path(initial.json()["bundle_path"])
    # Initial install lands at 0.1.0, so the per-version directory is
    # ``<bundles_dir>/<alias>/0.1.0/``.
    assert initial_bundle.name == "0.1.0"
    # Drop a marker file the new bundle directory should NOT contain
    # (per-version subdirs mean the old bundle stays intact).
    (initial_bundle / "old_marker.txt").write_text("from the old bundle")

    replace = await client.post(
        f"/api/admin/tools/{alias}/replace-bundle",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias, version="2.0"))},
        files=_files(
            alias=alias,
            descriptor_bytes=_make_descriptor_json(alias, version="2.0"),
        ),
    )
    assert replace.status_code == 200, replace.text
    # Major bump from 0.1.0 → 1.0.0 (server-controlled; supplied "2.0"
    # is ignored).
    assert replace.json()["version"] == "1.0.0"
    new_bundle = Path(replace.json()["bundle_path"])
    assert new_bundle.name == "1.0.0"
    # The new bundle is a fresh per-version directory; the original
    # one is untouched so any job that captured the 0.1.0 snapshot
    # still has its files.
    assert (initial_bundle / "old_marker.txt").exists()
    assert (new_bundle / "main.py").is_file()
    assert new_bundle != initial_bundle


@pytest.mark.asyncio
async def test_replace_tool_bundle_without_descriptor_keeps_inputs(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Replace-bundle accepts just a ZIP and keeps the existing inputs/outputs."""
    del seeded
    alias = "keep_io_tool"
    desc = json.loads(_make_descriptor_json(alias).decode())
    desc["inputs"] = [
        {"name": "widget", "label": "Widget", "type": "float", "default": 1.0, "required": True}
    ]
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias, descriptor_bytes=json.dumps(desc).encode("utf-8")),
    )
    assert install.status_code == 201, install.text

    # Replace with ONLY the static ZIP — no descriptor_json part at all.
    replace = await client.post(
        f"/api/admin/tools/{alias}/replace-bundle",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=[("static_zip", ("bundle.zip", _make_static_zip(), "application/zip"))],
    )
    assert replace.status_code == 200, replace.text
    assert replace.json()["version"] == "1.0.0"  # still a major bump

    # The tool's inputs survived the bundle swap.
    got = await client.get(f"/api/tools/{alias}", headers=auth_headers)
    assert got.status_code == 200, got.text
    assert [i["name"] for i in got.json()["descriptor"]["inputs"]] == ["widget"]


@pytest.mark.asyncio
async def test_replace_tool_bundle_rejects_alias_mismatch(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The descriptor JSON's alias must match the URL alias."""
    del seeded
    alias = "good_alias"
    create = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert create.status_code == 201

    refused = await client.post(
        f"/api/admin/tools/{alias}/replace-bundle",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata("evil_alias"))},
        files=_files(
            alias=alias,
            descriptor_bytes=_make_descriptor_json("evil_alias"),
        ),
    )
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_unarchive_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Archive then unarchive flips the row back to active."""
    alias = str(seeded["tool_alias"])
    archive = await client.post(
        f"/api/admin/tools/{alias}/archive",
        headers=auth_headers,
        json={"reason": "test"},
    )
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    unarchive = await client.post(
        f"/api/admin/tools/{alias}/unarchive",
        headers=auth_headers,
    )
    assert unarchive.status_code == 200
    assert unarchive.json()["archived_at"] is None


@pytest.mark.asyncio
async def test_archive_and_unarchive_appear_in_change_log(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Archive + re-activate surface in the change log (from the audit trail).

    They hold the version constant, so they can't have their own
    ``(alias, version)`` snapshot row — the change-log endpoint merges
    them in from the audit log instead.
    """
    alias = str(seeded["tool_alias"])
    await client.post(
        f"/api/admin/tools/{alias}/archive",
        headers=auth_headers,
        json={"reason": "retired for the test"},
    )
    await client.post(f"/api/admin/tools/{alias}/unarchive", headers=auth_headers)

    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    summaries = [e["change_summary"] for e in versions]
    assert any(s.startswith("Re-activated") for s in summaries), summaries
    assert any(s.startswith("Archived") for s in summaries), summaries
    # The archive entry carries its reason through to the field-diff map.
    archive_entry = next(e for e in versions if e["change_summary"].startswith("Archived"))
    assert archive_entry["changes"]["archived"] == {"old": False, "new": True}
    assert archive_entry["changes"]["archive_reason"]["new"] == "retired for the test"
    # Newest-first ordering holds across the merge: re-activate is most recent.
    assert versions[0]["change_summary"].startswith("Re-activated")


@pytest.mark.asyncio
async def test_unarchive_idempotent_on_active_tool(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Unarchiving an already-active tool is a no-op (200 OK, archived_at stays None)."""
    alias = str(seeded["tool_alias"])
    response = await client.post(
        f"/api/admin/tools/{alias}/unarchive",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["archived_at"] is None


@pytest.mark.asyncio
async def test_team_gated_tool_hidden_from_non_member(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A released tool tagged with a private team is invisible to non-members.

    Admins / super-users still see every tool — the team filter only
    applies to non-privileged users (anyone *without* tools.see_new_state).
    """
    del seeded
    # Tool A: public — tagged with World (seeded by migration 0014).
    public_alias = "public_world_tool"
    install_a = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(public_alias, teams=["World"]))},
        files=_files(alias=public_alias),
    )
    assert install_a.status_code == 201, install_a.text

    # Promote A through new → test → released so a regular user
    # without tools.see_new_state can see it.
    for state in ("test", "released"):
        r = await client.post(
            f"/api/admin/tools/{public_alias}/promote",
            headers=auth_headers,
            json={"new_state": state},
        )
        assert r.status_code == 200, r.text

    # Tool B: private — tagged with a team the regular user isn't in.
    team_create = await client.post(
        "/api/admin/teams",
        headers=auth_headers,
        json={"name": "Engineering", "description": ""},
    )
    assert team_create.status_code == 201

    private_alias = "private_eng_tool"
    install_b = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(private_alias, teams=["Engineering"]))},
        files=_files(alias=private_alias),
    )
    assert install_b.status_code == 201, install_b.text
    for state in ("test", "released"):
        r = await client.post(
            f"/api/admin/tools/{private_alias}/promote",
            headers=auth_headers,
            json={"new_state": state},
        )
        assert r.status_code == 200, r.text

    # Create a regular user with no team membership.
    pat = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "pat_outsider",
            "email": "pat@example.com",
            "first_name": "Pat",
            "last_name": "Outsider",
            "password": "Patspasswordnow1!",
            "role": "regular_user",
        },
    )
    assert pat.status_code == 201

    login = await client.post(
        "/api/auth/login",
        json={"username": "pat_outsider", "password": "Patspasswordnow1!"},
    )
    assert login.status_code == 200
    pat_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # /api/tools is the regular (non-admin) listing — applies the same
    # state + team filter the home-page browser uses.
    response = await client.get("/api/tools", headers=pat_headers)
    assert response.status_code == 200
    aliases = {t["alias"] for t in response.json()}
    assert public_alias in aliases  # World-tagged → visible to everyone
    assert private_alias not in aliases  # Engineering-tagged → hidden from non-members


@pytest.mark.asyncio
async def test_admin_tool_response_includes_approvers_and_teams(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The admin list now bundles the per-card summary fields:

    ``approver_usernames`` + ``teams`` drive the sole-approver
    pre-flight on /admin/users/{id}/edit, and the tools page renders
    Visible-by + Approvers + Downstream-tools lines from
    ``approvals_policy`` + ``downstream_tools``. All four are exposed
    here so neither page has to fetch the full descriptor.
    """
    alias = str(seeded["tool_alias"])
    response = await client.get("/api/admin/tools", headers=auth_headers)
    assert response.status_code == 200
    by_alias = {t["alias"]: t for t in response.json()}
    assert alias in by_alias
    row = by_alias[alias]
    assert isinstance(row["approver_usernames"], list)
    assert isinstance(row["teams"], list)
    assert isinstance(row["approvals_policy"], str)
    assert isinstance(row["downstream_tools"], list)


@pytest.mark.asyncio
async def test_set_tool_approvers_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Bulk-replacing approvers writes the descriptor and surfaces in the list."""
    # Need at least one approver-eligible user to set as the approver.
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "approver_pat",
            "email": "approver_pat@example.com",
            "first_name": "Pat",
            "last_name": "A",
            "password": "Patspasswordnow1!",
            "role": "regular_user",
        },
    )
    assert create.status_code == 201
    pat_id = create.json()["id"]
    # Flag them as eligible to be an approver.
    set_flag = await client.post(
        f"/api/admin/users/{pat_id}/set-approver",
        headers=auth_headers,
        json={"is_approver": True},
    )
    assert set_flag.status_code == 200

    # Set the seeded super-user's approver flag too so we have two
    # eligible approvers — install the tool with both, then prove
    # set-approvers can swap to just one of them.
    super_set = await client.post(
        f"/api/admin/users/{seeded['user_id']}/set-approver",
        headers=auth_headers,
        json={"is_approver": True},
    )
    assert super_set.status_code == 200

    # Install a fresh tool we control. Approvals policy must be non-none
    # before approver_usernames may be non-empty (descriptor invariant).
    alias = "approver_swap_tool"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={
            "metadata": json.dumps(
                _metadata(
                    alias,
                    approvals_policy="pre",
                    approver_usernames=[seeded["username"], "approver_pat"],
                )
            )
        },
        files=_files(alias=alias),
    )
    assert install.status_code == 201, install.text

    swap = await client.post(
        f"/api/admin/tools/{alias}/set-approvers",
        headers=auth_headers,
        json={"approver_usernames": ["approver_pat"]},
    )
    assert swap.status_code == 200, swap.text
    assert swap.json()["approver_usernames"] == ["approver_pat"]

    # And the listing reflects it without a separate fetch.
    listing = await client.get("/api/admin/tools", headers=auth_headers)
    by_alias = {t["alias"]: t for t in listing.json()}
    assert by_alias[alias]["approver_usernames"] == ["approver_pat"]


# --- C1: in-browser file edit (minor bump) -------------------------------


@pytest.mark.asyncio
async def test_save_file_minor_bumps_and_preserves_old_version(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Saving a file edit clones the bundle to a new per-version dir + minor bumps.

    The old version's directory is left intact on disk so a job that
    captured the previous ``tool_version_id`` keeps resolving against
    its original files. Asserts both: new-version dir exists with the
    edit, old-version dir still exists with the original contents.
    """
    del seeded
    alias = "edit_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201, install.text
    assert install.json()["version"] == "0.1.0"
    initial_bundle = Path(install.json()["bundle_path"])
    assert initial_bundle.name == "0.1.0"
    assert (initial_bundle / "main.py").read_text() == "# placeholder\n"

    save = await client.post(
        f"/api/admin/tools/{alias}/files/main.py",
        headers=auth_headers,
        json={"contents": "# edited from the in-browser editor\n"},
    )
    assert save.status_code == 200, save.text
    body = save.json()
    assert body["version"] == "0.2.0"  # minor bump
    new_bundle = Path(body["bundle_path"])
    assert new_bundle.name == "0.2.0"
    assert new_bundle != initial_bundle

    assert (new_bundle / "main.py").read_text() == "# edited from the in-browser editor\n"
    assert initial_bundle.exists()
    assert (initial_bundle / "main.py").read_text() == "# placeholder\n"

    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    assert len(versions) == 2  # install + edit
    edit_entry = versions[0]
    assert edit_entry["version"] == "0.2.0"
    assert "minor bump" in edit_entry["change_summary"].lower()
    assert "main.py" in edit_entry["changes"]
    assert edit_entry["changes"]["main.py"]["old"] == "# placeholder\n"
    assert edit_entry["changes"]["main.py"]["new"] == "# edited from the in-browser editor\n"


@pytest.mark.asyncio
async def test_save_file_no_op_returns_existing_row(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Submitting unchanged contents short-circuits — no bump, no snapshot."""
    del seeded
    alias = "noop_edit"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    same = await client.post(
        f"/api/admin/tools/{alias}/files/main.py",
        headers=auth_headers,
        json={"contents": "# placeholder\n"},
    )
    assert same.status_code == 200
    assert same.json()["version"] == "0.1.0"  # no bump

    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    assert len(versions) == 1  # only the install snapshot


@pytest.mark.asyncio
async def test_save_file_rejects_path_traversal(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Edits whose resolved path escapes the bundle dir are 400."""
    del seeded
    alias = "traversal_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    refused = await client.post(
        f"/api/admin/tools/{alias}/files/..%2Fsneaky.txt",
        headers=auth_headers,
        json={"contents": "evil"},
    )
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_save_file_rejects_descriptor_json(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """descriptor.json edits are refused — operator must use the metadata form."""
    del seeded
    alias = "descriptor_carve_out"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    refused = await client.post(
        f"/api/admin/tools/{alias}/files/descriptor.json",
        headers=auth_headers,
        json={"contents": "{}"},
    )
    assert refused.status_code == 400
    assert "metadata form" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_save_file_rejects_missing_path(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Editing a path that doesn't exist in the bundle is 400 with a nudge."""
    del seeded
    alias = "missing_file_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    refused = await client.post(
        f"/api/admin/tools/{alias}/files/brand_new.py",
        headers=auth_headers,
        json={"contents": "print('new')\n"},
    )
    assert refused.status_code == 400
    assert "doesn't exist" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_list_files_includes_main_py(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """GET /admin/tools/{alias}/files lists every file in the current bundle."""
    del seeded
    alias = "file_listing_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    listing = await client.get(f"/api/admin/tools/{alias}/files", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    paths = {f["relative_path"] for f in body["files"]}
    assert "main.py" in paths
    assert "descriptor.json" in paths
    main_entry = next(f for f in body["files"] if f["relative_path"] == "main.py")
    assert main_entry["is_text"] is True


@pytest.mark.asyncio
async def test_read_file_returns_contents(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """GET /admin/tools/{alias}/files/{path} returns UTF-8 contents."""
    del seeded
    alias = "file_read_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    read = await client.get(f"/api/admin/tools/{alias}/files/main.py", headers=auth_headers)
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["relative_path"] == "main.py"
    assert body["contents"] == "# placeholder\n"


# --- C2: server-side syntax validation ----------------------------------


@pytest.mark.asyncio
async def test_save_file_rejects_python_syntax_error(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A Python edit with a SyntaxError → 422 + structured errors, no version bump.

    The bundle on disk must still be the original — the syntax-validation
    check runs before any copy / write so a broken edit never lands.
    """
    del seeded
    alias = "py_syntax_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201
    initial_bundle = Path(install.json()["bundle_path"])
    assert initial_bundle.name == "0.1.0"

    save = await client.post(
        f"/api/admin/tools/{alias}/files/main.py",
        headers=auth_headers,
        json={"contents": "def broken(:\n    pass\n"},
    )
    assert save.status_code == 422, save.text
    body = save.json()
    assert body["path"] == "main.py"
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) == 1
    issue = body["errors"][0]
    assert issue["line"] >= 1
    assert issue.get("message")

    # Tool stayed at 0.1.0; only the install snapshot exists.
    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    assert detail.json()["version"] == "0.1.0"
    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    assert len(versions) == 1
    # The bundle directory for the would-be 0.2.0 was never created.
    assert not (initial_bundle.parent / "0.2.0").exists()


@pytest.mark.asyncio
async def test_save_file_rejects_invalid_json_payload(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A JSON payload file with broken JSON → 422 + line/col diagnostics.

    The bundle ZIP staged by these tests has only main.py + descriptor.json,
    so we add a presets file via descriptor.json's metadata path and edit
    that. Simpler approach: stage a tool with an extra .json and edit it.
    """
    del seeded
    alias = "json_syntax_target"
    extra_zip_buf = io.BytesIO()
    with zipfile.ZipFile(extra_zip_buf, "w") as zf:
        zf.writestr("main.py", b"# placeholder\n")
        zf.writestr("config.json", b'{"key": "value"}\n')

    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias, zip_bytes=extra_zip_buf.getvalue()),
    )
    assert install.status_code == 201

    save = await client.post(
        f"/api/admin/tools/{alias}/files/config.json",
        headers=auth_headers,
        json={"contents": '{"key": "value",}\n'},  # trailing comma
    )
    assert save.status_code == 422, save.text
    body = save.json()
    assert body["path"] == "config.json"
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) == 1


@pytest.mark.asyncio
async def test_save_file_accepts_valid_python(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A syntactically-valid Python edit goes through the syntax gate cleanly."""
    del seeded
    alias = "py_valid_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201

    save = await client.post(
        f"/api/admin/tools/{alias}/files/main.py",
        headers=auth_headers,
        json={"contents": "def fine():\n    return 42\n"},
    )
    assert save.status_code == 200, save.text
    assert save.json()["version"] == "0.2.0"


@pytest.mark.asyncio
async def test_update_descriptor_with_wizard_fields_overlays_inputs_outputs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """The wizard's edit flow patch-bumps + persists new ``inputs[]`` / ``outputs[]``.

    POST /admin/tools/{alias}/update-descriptor with the optional
    descriptor-body fields (``inputs``, ``outputs``, ``input_groups``,
    etc.) overlays them onto the stored descriptor and patch-bumps
    the version. Without these fields the existing structure stays
    in place — backwards-compatible with metadata-only callers.
    """
    del seeded
    alias = "wizard_edit_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201, install.text
    assert install.json()["version"] == "0.1.0"

    body: dict[str, Any] = {
        "name": _metadata(alias)["name"],
        "version": "ignored",
        "short_description": _metadata(alias)["short_description"],
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
        # Wizard payload — overlay onto the existing descriptor.
        "input_groups": [
            {"name": "main", "label": "Main", "column": 1, "row": 1, "sub_columns": 1}
        ],
        "inputs": [
            {
                "name": "pressure",
                "label": "Pressure",
                "type": "float",
                "unit": "MPa",
                "required": True,
                "minimum": 0.0,
                "maximum": 1000.0,
            }
        ],
        "retain_files": ["*.csv"],
    }
    update = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json=body,
    )
    assert update.status_code == 200, update.text
    assert update.json()["version"] == "0.1.1"  # patch bump

    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    descriptor = detail.json()["descriptor"]
    assert len(descriptor["inputs"]) == 1
    assert descriptor["inputs"][0]["name"] == "pressure"
    assert descriptor["inputs"][0]["unit"] == "MPa"
    assert descriptor["input_groups"][0]["name"] == "main"
    assert descriptor["retain_files"] == ["*.csv"]


@pytest.mark.asyncio
async def test_update_descriptor_accepts_and_persists_dependencies(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A §16.16 dependency rule survives the wizard's edit POST.

    Regression: the edit DTO previously rejected ``dependencies`` with
    ``extra_forbidden`` (it was never added when §16.16 landed), so
    authoring a rule in the wizard 422'd on save.
    """
    del seeded
    alias = "dep_edit_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201, install.text

    body: dict[str, Any] = {
        "name": _metadata(alias)["name"],
        "version": "ignored",
        "short_description": _metadata(alias)["short_description"],
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
        "inputs": [
            {"name": "density_left", "label": "Density L", "type": "float"},
            {"name": "density_right", "label": "Density R", "type": "float"},
        ],
        # The exact rule shape the wizard sent that used to 422.
        "dependencies": [
            {
                "when": "density_left > 1500",
                "effects": [{"kind": "set_value", "field": "density_right", "value": 2000.0}],
            }
        ],
    }
    update = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json=body,
    )
    assert update.status_code == 200, update.text

    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    descriptor = detail.json()["descriptor"]
    assert descriptor["dependencies"] == [
        {
            "when": "density_left > 1500",
            "effects": [
                {
                    "kind": "set_value",
                    "field": "density_right",
                    "value": 2000.0,
                    "value_expr": None,
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_update_descriptor_dependency_only_edit_persists_and_bumps(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """An edit that changes *only* the dependency rules isn't swallowed.

    Regression: ``dependencies`` wasn't in ``METADATA_FIELDS``, so a
    dependency-only edit diffed to nothing and ``update_descriptor``
    short-circuited as a no-op — the rules were silently discarded and
    the Save button looked like it did nothing.
    """
    del seeded
    alias = "dep_only_edit"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201, install.text

    base_body: dict[str, Any] = {
        "name": _metadata(alias)["name"],
        "version": "ignored",
        "short_description": _metadata(alias)["short_description"],
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
        "inputs": [
            {"name": "mode", "label": "Mode", "type": "select", "options": ["a", "b"]},
            {"name": "threshold", "label": "Threshold", "type": "float"},
        ],
    }
    # 1. Establish the inputs — a real change, bumps 0.1.0 → 0.1.1.
    first = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor", headers=auth_headers, json=base_body
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == "0.1.1"

    # 2. Change ONLY the dependency rules; every other field identical.
    rule = {
        "when": "mode == 'a'",
        "effects": [
            {"kind": "set_value", "field": "threshold", "value": 2.0, "value_expr": None},
            {"kind": "disable", "field": "threshold"},
        ],
    }
    second = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json={**base_body, "dependencies": [rule]},
    )
    assert second.status_code == 200, second.text
    # Not a no-op: patch-bumped again and the rule actually persisted.
    assert second.json()["version"] == "0.1.2"
    descriptor = (
        await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    ).json()["descriptor"]
    assert descriptor["dependencies"] == [rule]
    # A change-log snapshot was written for the dependency edit
    # (install + inputs edit + dependency edit = 3).
    versions = (
        await client.get(f"/api/admin/tools/{alias}/versions", headers=auth_headers)
    ).json()["versions"]
    assert len(versions) == 3


@pytest.mark.asyncio
async def test_update_descriptor_accepts_and_persists_group_expansion_flags(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """The wizard's section-behaviour toggles round-trip through edit.

    Regression: the edit body schema forbids extra inputs, so the two
    ``input_groups_start_collapsed`` / ``require_all_groups_opened``
    flags the wizard always sends used to 422 with ``extra_forbidden``.
    They must be accepted, overlaid onto the descriptor, and — when a
    later edit omits them — left unchanged.
    """
    del seeded
    alias = "group_flags_target"
    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias),
    )
    assert install.status_code == 201, install.text

    base_body: dict[str, Any] = {
        "name": _metadata(alias)["name"],
        "version": "ignored",
        "short_description": _metadata(alias)["short_description"],
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

    # Edit that flips both flags off — previously rejected with 422.
    update = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json={
            **base_body,
            "input_groups_start_collapsed": False,
            "require_all_groups_opened": False,
        },
    )
    assert update.status_code == 200, update.text

    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    descriptor = detail.json()["descriptor"]
    assert descriptor["input_groups_start_collapsed"] is False
    assert descriptor["require_all_groups_opened"] is False

    # A later metadata-only edit omits the flags → they stay as set
    # (the optional "absent means leave unchanged" contract).
    update2 = await client.post(
        f"/api/admin/tools/{alias}/update-descriptor",
        headers=auth_headers,
        json={**base_body, "short_description": "Metadata-only edit."},
    )
    assert update2.status_code == 200, update2.text
    detail2 = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    descriptor2 = detail2.json()["descriptor"]
    assert descriptor2["input_groups_start_collapsed"] is False
    assert descriptor2["require_all_groups_opened"] is False


@pytest.mark.asyncio
async def test_save_file_skips_validation_for_unknown_extension(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Files without a registered parser (.md, .txt) save without checks.

    A markdown file with text that *would* be invalid as JSON or Python
    saves cleanly — the syntax-checker only runs for languages we know
    how to parse.
    """
    del seeded
    alias = "md_target"
    extra_zip_buf = io.BytesIO()
    with zipfile.ZipFile(extra_zip_buf, "w") as zf:
        zf.writestr("main.py", b"# placeholder\n")
        zf.writestr("README.md", b"# Tool README\n")

    install = await client.post(
        "/api/admin/tools",
        headers=auth_headers,
        data={"metadata": json.dumps(_metadata(alias))},
        files=_files(alias=alias, zip_bytes=extra_zip_buf.getvalue()),
    )
    assert install.status_code == 201

    save = await client.post(
        f"/api/admin/tools/{alias}/files/README.md",
        headers=auth_headers,
        json={"contents": "# Edited\n\n{not valid json or python\n"},
    )
    assert save.status_code == 200, save.text
    assert save.json()["version"] == "0.2.0"


@pytest.mark.asyncio
async def test_set_and_clear_tool_python_env(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Pin the seeded tool to a registered env, then clear it back to default."""
    alias = seeded["tool_alias"]

    # Tool starts with no env pin → GET detail surfaces None.
    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["python_env_id"] is None

    # Register an env to pin to.
    env = await client.post(
        "/api/admin/python-envs",
        headers=auth_headers,
        json={"name": "py312", "interpreter_path": "/opt/envs/py312/bin/python"},
    )
    assert env.status_code == 201, env.text
    env_id = env.json()["id"]

    # Pin it — row-level set, no version bump.
    before_version = detail.json()["version"]
    pin = await client.post(
        f"/api/admin/tools/{alias}/python-env",
        headers=auth_headers,
        json={"python_env_id": env_id},
    )
    assert pin.status_code == 200, pin.text
    assert pin.json()["version"] == before_version  # no descriptor bump

    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    assert detail.json()["python_env_id"] == env_id

    # Clear the pin → back to None (default → main MATOFX env).
    clear = await client.post(
        f"/api/admin/tools/{alias}/python-env",
        headers=auth_headers,
        json={"python_env_id": None},
    )
    assert clear.status_code == 200, clear.text
    detail = await client.get(f"/api/admin/tools/{alias}", headers=auth_headers)
    assert detail.json()["python_env_id"] is None


@pytest.mark.asyncio
async def test_set_tool_python_env_rejects_unknown_env(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Pinning to a non-existent env id is a 400 with a friendly message."""
    alias = seeded["tool_alias"]
    response = await client.post(
        f"/api/admin/tools/{alias}/python-env",
        headers=auth_headers,
        json={"python_env_id": 99999},
    )
    assert response.status_code == 400, response.text
    assert "isn't available" in response.json()["detail"]
