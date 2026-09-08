"""Admin-router tests: users / teams / tools."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# --- users -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/users")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_users_returns_seeded(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    r = await client.get("/api/admin/users", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any(u["username"] == seeded["username"] for u in body)


@pytest.mark.asyncio
async def test_create_user_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "alice",
            "email": "alice@example.com",
            "full_name": "Alice Tester",
            "password": "Alicepassword1!",
            "role": "regular_user",
            "is_approver": False,
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["username"] == "alice"
    assert body["role"] == "regular_user"
    assert body["is_active"] is True

    # Now visible in the list.
    listing = await client.get("/api/admin/users", headers=auth_headers)
    aliases = [u["username"] for u in listing.json()]
    assert "alice" in aliases


@pytest.mark.asyncio
async def test_deactivate_then_reactivate(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "bob",
            "email": "bob@example.com",
            "full_name": "Bob",
            "password": "Bobpassword1!",
            "role": "regular_user",
        },
    )
    assert create.status_code == 201
    user_id = create.json()["id"]

    deact = await client.post(f"/api/admin/users/{user_id}/deactivate", headers=auth_headers)
    assert deact.status_code == 200
    assert deact.json()["is_active"] is False

    react = await client.post(f"/api/admin/users/{user_id}/reactivate", headers=auth_headers)
    assert react.status_code == 200
    assert react.json()["is_active"] is True


@pytest.mark.asyncio
async def test_set_approver_flag(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "carol",
            "email": "carol@example.com",
            "full_name": "Carol",
            "password": "Carolpassword1!",
            "role": "regular_user",
        },
    )
    user_id = create.json()["id"]
    r = await client.post(
        f"/api/admin/users/{user_id}/set-approver",
        headers=auth_headers,
        json={"is_approver": True},
    )
    assert r.status_code == 200
    assert r.json()["is_approver"] is True


# --- roles -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_roles_returns_built_ins(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    r = await client.get("/api/admin/roles", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    names = {row["name"] for row in body}
    assert {"super_user", "administrator", "regular_user"} <= names
    # ``user_count`` is bundled in for the admin page's role panel.
    by_name = {row["name"]: row for row in body}
    assert isinstance(by_name["super_user"]["user_count"], int)
    assert by_name["super_user"]["user_count"] >= 1  # the seeded super-user


@pytest.mark.asyncio
async def test_create_custom_role(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Create a non-built-in role with a curated permission set."""
    del seeded
    create = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={
            "name": "tools_steward",
            "description": "May install and promote tools.",
            "permissions": ["tools.install", "tools.promote_state"],
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "tools_steward"
    assert body["is_built_in"] is False
    assert sorted(body["permissions"]) == ["tools.install", "tools.promote_state"]
    assert body["user_count"] == 0


@pytest.mark.asyncio
async def test_create_capability_role(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A capability group is created with kind='capability' and curate perms."""
    del seeded
    create = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={
            "name": "docs_curator",
            "description": "Curates library docs.",
            "permissions": ["library.curate"],
            "kind": "capability",
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["kind"] == "capability"
    assert body["permissions"] == ["library.curate"]


@pytest.mark.asyncio
async def test_create_role_rejects_cross_kind_permission(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A base group can't hold a curate permission (400)."""
    del seeded
    bad = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={
            "name": "greedy_base",
            "description": "",
            "permissions": ["materials.curate"],
            "kind": "base",
        },
    )
    assert bad.status_code == 400, bad.text


@pytest.mark.asyncio
async def test_permissions_catalog_flags_capability(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The catalog marks *.curate slugs as capability permissions."""
    del seeded
    r = await client.get("/api/admin/permissions", headers=auth_headers)
    assert r.status_code == 200
    by_slug = {entry["slug"]: entry for entry in r.json()}
    assert by_slug["library.curate"]["is_capability"] is True
    assert by_slug["materials.curate"]["is_capability"] is True
    assert by_slug["tools.install"]["is_capability"] is False


@pytest.mark.asyncio
async def test_create_role_rejects_built_in_name(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A built-in name (e.g. ``regular_user``) is reserved → 409."""
    del seeded
    clash = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={
            "name": "regular_user",
            "description": "",
            "permissions": [],
        },
    )
    assert clash.status_code == 409


@pytest.mark.asyncio
async def test_set_role_permissions_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Replacing a custom role's permission set updates the API view."""
    del seeded
    create = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={
            "name": "swapper",
            "description": "",
            "permissions": ["tools.install"],
        },
    )
    role_id = create.json()["id"]

    update = await client.post(
        f"/api/admin/roles/{role_id}/set-permissions",
        headers=auth_headers,
        json={"permissions": ["projects.see_all", "tools.promote_state"]},
    )
    assert update.status_code == 200, update.text
    assert sorted(update.json()["permissions"]) == [
        "projects.see_all",
        "tools.promote_state",
    ]


@pytest.mark.asyncio
async def test_super_user_permissions_locked(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Editing super_user's permission set is refused (403, not 409 — code lock)."""
    del seeded
    listing = await client.get("/api/admin/roles", headers=auth_headers)
    super_user_id = next(r["id"] for r in listing.json() if r["name"] == "super_user")

    refused = await client.post(
        f"/api/admin/roles/{super_user_id}/set-permissions",
        headers=auth_headers,
        json={"permissions": ["projects.see_all"]},
    )
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_update_role_description(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Description edit applies to built-ins too (only the *name* is locked)."""
    del seeded
    listing = await client.get("/api/admin/roles", headers=auth_headers)
    regular_id = next(r["id"] for r in listing.json() if r["name"] == "regular_user")

    update = await client.post(
        f"/api/admin/roles/{regular_id}/update-description",
        headers=auth_headers,
        json={"description": "Tuned for our pilot deployment."},
    )
    assert update.status_code == 200
    assert update.json()["description"] == "Tuned for our pilot deployment."


@pytest.mark.asyncio
async def test_delete_custom_role_refused(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Even an empty custom group is permanent — delete returns 409 for audit."""
    del seeded
    create = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={"name": "throwaway", "description": "", "permissions": []},
    )
    role_id = create.json()["id"]
    refused = await client.delete(f"/api/admin/roles/{role_id}", headers=auth_headers)
    assert refused.status_code == 409
    assert "permanently" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_delete_built_in_role_refused(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Built-in roles can't be deleted — returns 409."""
    del seeded
    listing = await client.get("/api/admin/roles", headers=auth_headers)
    regular_id = next(r["id"] for r in listing.json() if r["name"] == "regular_user")
    refused = await client.delete(f"/api/admin/roles/{regular_id}", headers=auth_headers)
    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_delete_in_use_role_refused(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Deleting a role still assigned to a user returns 409 (groups are permanent)."""
    del seeded
    create_role = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={"name": "bound", "description": "", "permissions": []},
    )
    role_id = create_role.json()["id"]

    create_user = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "ned",
            "email": "ned@example.com",
            "first_name": "Ned",
            "last_name": "S",
            "password": "Nedpassword1!",
            "role": "bound",
        },
    )
    assert create_user.status_code == 201

    refused = await client.delete(f"/api/admin/roles/{role_id}", headers=auth_headers)
    assert refused.status_code == 409
    assert "permanently" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_list_permissions_catalog(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The permissions endpoint returns the full closed catalog with descriptions."""
    del seeded
    r = await client.get("/api/admin/permissions", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    slugs = {entry["slug"] for entry in body}
    # Spot-check a few permissions that we know exist in the catalog.
    assert "projects.see_all" in slugs
    assert "tools.install" in slugs
    assert "users.manage" in slugs
    assert "database.explore" in slugs
    # Each entry has a label + a meaningful, per-permission description —
    # not the catalog's class docstring repeated for every slug (the bug
    # that surfaced when the endpoint read each enum member's __doc__).
    descriptions = {entry["slug"]: entry["description"] for entry in body}
    for entry in body:
        assert entry["label"]
        assert isinstance(entry["description"], str)
        assert entry["description"].strip()
        assert "Closed catalog of permission slugs" not in entry["description"]
    # Distinct slugs get distinct descriptions (no copy-paste fallback).
    assert len(set(descriptions.values())) == len(descriptions)


# --- teams -----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_team_then_add_member_then_remove(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    # Need a non-super-user to add as a member.
    user_create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "dave",
            "email": "dave@example.com",
            "full_name": "Dave",
            "password": "Davepassword1!",
            "role": "regular_user",
        },
    )
    user_id = user_create.json()["id"]
    del seeded

    create = await client.post(
        "/api/admin/teams",
        headers=auth_headers,
        json={"name": "engineering", "description": "the team"},
    )
    assert create.status_code == 201, create.text
    team_id = create.json()["id"]
    assert create.json()["member_count"] == 0

    add = await client.post(
        f"/api/admin/teams/{team_id}/members",
        headers=auth_headers,
        json={"user_id": user_id},
    )
    assert add.status_code == 204

    members = await client.get(f"/api/admin/teams/{team_id}/members", headers=auth_headers)
    assert members.status_code == 200
    assert [m["username"] for m in members.json()] == ["dave"]

    remove = await client.delete(
        f"/api/admin/teams/{team_id}/members/{user_id}", headers=auth_headers
    )
    assert remove.status_code == 204

    members2 = await client.get(f"/api/admin/teams/{team_id}/members", headers=auth_headers)
    assert members2.json() == []


@pytest.mark.asyncio
async def test_rename_team(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """PUT renames a team; a clash with another team's name returns 409."""
    del seeded
    a = await client.post(
        "/api/admin/teams", headers=auth_headers, json={"name": "alpha", "description": ""}
    )
    team_id = a.json()["id"]
    await client.post(
        "/api/admin/teams", headers=auth_headers, json={"name": "beta", "description": ""}
    )

    renamed = await client.put(
        f"/api/admin/teams/{team_id}",
        headers=auth_headers,
        json={"name": "alpha-renamed", "description": "now tidy"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "alpha-renamed"
    assert renamed.json()["description"] == "now tidy"

    clash = await client.put(
        f"/api/admin/teams/{team_id}",
        headers=auth_headers,
        json={"name": "beta", "description": ""},
    )
    assert clash.status_code == 409


@pytest.mark.asyncio
async def test_bulk_add_team_members(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Several users join a team in one call; re-adding is a silent no-op."""
    del seeded
    ids = []
    for name in ("u1", "u2", "u3"):
        created = await client.post(
            "/api/admin/users",
            headers=auth_headers,
            json={
                "username": name,
                "email": f"{name}@example.com",
                "full_name": name,
                "password": "Bulkpassword1!",
                "role": "regular_user",
            },
        )
        ids.append(created.json()["id"])

    team = await client.post(
        "/api/admin/teams", headers=auth_headers, json={"name": "bulk", "description": ""}
    )
    team_id = team.json()["id"]

    bulk = await client.post(
        f"/api/admin/teams/{team_id}/members/bulk",
        headers=auth_headers,
        json={"user_ids": ids},
    )
    assert bulk.status_code == 204
    members = await client.get(f"/api/admin/teams/{team_id}/members", headers=auth_headers)
    assert {m["username"] for m in members.json()} == {"u1", "u2", "u3"}

    # Re-running with an overlapping set doesn't error.
    again = await client.post(
        f"/api/admin/teams/{team_id}/members/bulk",
        headers=auth_headers,
        json={"user_ids": ids},
    )
    assert again.status_code == 204


@pytest.mark.asyncio
async def test_rename_custom_role_and_collisions(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Custom role renames; clashing with another role or a built-in returns 409."""
    del seeded
    one = await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={"name": "role-one", "description": "", "permissions": []},
    )
    role_id = one.json()["id"]
    await client.post(
        "/api/admin/roles",
        headers=auth_headers,
        json={"name": "role-two", "description": "", "permissions": []},
    )

    ok = await client.post(
        f"/api/admin/roles/{role_id}/rename",
        headers=auth_headers,
        json={"name": "role-renamed"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "role-renamed"

    clash = await client.post(
        f"/api/admin/roles/{role_id}/rename",
        headers=auth_headers,
        json={"name": "role-two"},
    )
    assert clash.status_code == 409

    builtin = await client.post(
        f"/api/admin/roles/{role_id}/rename",
        headers=auth_headers,
        json={"name": "administrator"},
    )
    assert builtin.status_code == 409


@pytest.mark.asyncio
async def test_rename_built_in_role_refused(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Built-in role names are locked — renaming returns 409."""
    del seeded
    listing = await client.get("/api/admin/roles", headers=auth_headers)
    regular_id = next(r["id"] for r in listing.json() if r["name"] == "regular_user")
    refused = await client.post(
        f"/api/admin/roles/{regular_id}/rename",
        headers=auth_headers,
        json={"name": "basic_user"},
    )
    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_bulk_add_capability_members(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Capabilities take many members at once; a base role returns 400."""
    del seeded
    ids = []
    for name in ("c1", "c2"):
        created = await client.post(
            "/api/admin/users",
            headers=auth_headers,
            json={
                "username": name,
                "email": f"{name}@example.com",
                "full_name": name,
                "password": "Cappassword1!",
                "role": "regular_user",
            },
        )
        ids.append(created.json()["id"])

    listing = await client.get("/api/admin/roles", headers=auth_headers)
    cap_id = next(r["id"] for r in listing.json() if r["name"] == "library_curator")
    base_id = next(r["id"] for r in listing.json() if r["name"] == "regular_user")

    bulk = await client.post(
        f"/api/admin/roles/{cap_id}/members/bulk",
        headers=auth_headers,
        json={"user_ids": ids},
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["user_count"] == 2

    refused = await client.post(
        f"/api/admin/roles/{base_id}/members/bulk",
        headers=auth_headers,
        json={"user_ids": ids},
    )
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_remove_capability_member(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A single user can be removed from a capability; a base role returns 400."""
    del seeded
    created = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "capdrop",
            "email": "capdrop@example.com",
            "full_name": "Cap Drop",
            "password": "Cappassword1!",
            "role": "regular_user",
        },
    )
    user_id = created.json()["id"]

    listing = await client.get("/api/admin/roles", headers=auth_headers)
    cap_id = next(r["id"] for r in listing.json() if r["name"] == "library_curator")
    base_id = next(r["id"] for r in listing.json() if r["name"] == "regular_user")

    added = await client.post(
        f"/api/admin/roles/{cap_id}/members/bulk",
        headers=auth_headers,
        json={"user_ids": [user_id]},
    )
    assert added.json()["user_count"] == 1

    removed = await client.delete(
        f"/api/admin/roles/{cap_id}/members/{user_id}",
        headers=auth_headers,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["user_count"] == 0

    # Idempotent — removing again is a no-op, still 200.
    again = await client.delete(
        f"/api/admin/roles/{cap_id}/members/{user_id}",
        headers=auth_headers,
    )
    assert again.status_code == 200, again.text

    # A base role can't have members dropped this way.
    refused = await client.delete(
        f"/api/admin/roles/{base_id}/members/{user_id}",
        headers=auth_headers,
    )
    assert refused.status_code == 400


# --- tools -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_admin_tools_includes_seeded(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    r = await client.get("/api/admin/tools", headers=auth_headers)
    assert r.status_code == 200
    aliases = [t["alias"] for t in r.json()]
    assert seeded["tool_alias"] in aliases


@pytest.mark.asyncio
async def test_archive_tool(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    alias = seeded["tool_alias"]
    r = await client.post(
        f"/api/admin/tools/{alias}/archive",
        headers=auth_headers,
        json={"reason": "test"},
    )
    assert r.status_code == 200
    assert r.json()["archived_at"] is not None


# --- create with split name + teams ----------------------------------


@pytest.mark.asyncio
async def test_create_user_with_first_last_and_teams(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """First / last derive full_name; team_ids assigns membership at create."""
    del seeded
    eng = await client.post(
        "/api/admin/teams",
        headers=auth_headers,
        json={"name": "Engineering", "description": ""},
    )
    assert eng.status_code == 201
    qa = await client.post(
        "/api/admin/teams",
        headers=auth_headers,
        json={"name": "QA", "description": ""},
    )
    assert qa.status_code == 201
    team_ids = [eng.json()["id"], qa.json()["id"]]

    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "ellen",
            "email": "ellen@example.com",
            "first_name": "Ellen",
            "last_name": "O'Brien",
            "password": "Ellenpassword1!",
            "role": "regular_user",
            "team_ids": team_ids,
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["first_name"] == "Ellen"
    assert body["last_name"] == "O'Brien"
    # full_name auto-derives when not supplied.
    assert body["full_name"] == "Ellen O'Brien"
    assigned = sorted(t["name"] for t in body["teams"])
    assert assigned == ["Engineering", "QA"]


@pytest.mark.asyncio
async def test_create_user_rejects_duplicate_username(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Re-creating with the same username returns 409 + a friendly detail."""
    del seeded
    body = {
        "username": "lisa",
        "email": "lisa@example.com",
        "first_name": "Lisa",
        "last_name": "Q",
        "password": "Lisapassword1!",
        "role": "regular_user",
    }
    first = await client.post("/api/admin/users", headers=auth_headers, json=body)
    assert first.status_code == 201, first.text

    # Same username, different email — should still 409 on the username clash.
    clash = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={**body, "email": "lisa.q@example.com"},
    )
    assert clash.status_code == 409
    assert "already in use" in clash.json()["detail"]


@pytest.mark.asyncio
async def test_create_user_rejects_duplicate_email(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A second user with the same email but a new username also returns 409."""
    del seeded
    body = {
        "username": "mark",
        "email": "shared@example.com",
        "first_name": "Mark",
        "last_name": "R",
        "password": "Markpassword1!",
        "role": "regular_user",
    }
    first = await client.post("/api/admin/users", headers=auth_headers, json=body)
    assert first.status_code == 201, first.text

    clash = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={**body, "username": "mark2"},
    )
    assert clash.status_code == 409
    assert "already in use" in clash.json()["detail"]


# --- update-identity --------------------------------------------------


@pytest.mark.asyncio
async def test_update_identity_changes_name_and_email(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "fred",
            "email": "fred@example.com",
            "first_name": "Fred",
            "last_name": "Smith",
            "password": "Fredpassword1!",
            "role": "regular_user",
        },
    )
    user_id = create.json()["id"]

    update = await client.post(
        f"/api/admin/users/{user_id}/update-identity",
        headers=auth_headers,
        json={
            "first_name": "Frederick",
            "last_name": "Smith-Jones",
            "full_name": "",
            "email": "frederick@example.com",
        },
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["first_name"] == "Frederick"
    assert body["last_name"] == "Smith-Jones"
    assert body["email"] == "frederick@example.com"
    # Auto-derived from first + last because full_name was blank.
    assert body["full_name"] == "Frederick Smith-Jones"


@pytest.mark.asyncio
async def test_update_identity_rejects_duplicate_email(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Renaming user A's email to user B's value returns 409 with a friendly message."""
    del seeded
    a = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "gary",
            "email": "gary@example.com",
            "first_name": "Gary",
            "last_name": "X",
            "password": "Garypassword1!",
            "role": "regular_user",
        },
    )
    b = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "henry",
            "email": "henry@example.com",
            "first_name": "Henry",
            "last_name": "Y",
            "password": "Henrypassword1!",
            "role": "regular_user",
        },
    )
    assert a.status_code == 201
    assert b.status_code == 201

    clash = await client.post(
        f"/api/admin/users/{a.json()['id']}/update-identity",
        headers=auth_headers,
        json={
            "first_name": "Gary",
            "last_name": "X",
            "full_name": "",
            "email": "henry@example.com",
        },
    )
    assert clash.status_code == 409


@pytest.mark.asyncio
async def test_update_identity_permission_gate(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Without users.manage the endpoint refuses 403."""
    user_id = int(seeded["user_id"])
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "no_identity_edit",
            "email": "no_identity_edit@example.com",
            "first_name": "No",
            "last_name": "Edit",
            "password": "No-edit-password1!",
            "role": "regular_user",
        },
    )
    assert create.status_code == 201
    login = await client.post(
        "/api/auth/login",
        json={"username": "no_identity_edit", "password": "No-edit-password1!"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    forbidden = await client.post(
        f"/api/admin/users/{user_id}/update-identity",
        headers=headers,
        json={
            "first_name": "Mal",
            "last_name": "Adjusted",
            "full_name": "",
            "email": "shouldnt-go-through@example.com",
        },
    )
    assert forbidden.status_code == 403


# --- set-password -----------------------------------------------------


@pytest.mark.asyncio
async def test_set_password_rotates_hash_and_lets_user_log_in(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Admin reset rotates the password; user can sign in with the new one."""
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "ivy",
            "email": "ivy@example.com",
            "first_name": "Ivy",
            "last_name": "Z",
            "password": "Ivypassword1!",
            "role": "regular_user",
        },
    )
    user_id = create.json()["id"]

    reset = await client.post(
        f"/api/admin/users/{user_id}/set-password",
        headers=auth_headers,
        json={"password": "Ivy-new-password1!"},
    )
    assert reset.status_code == 200, reset.text

    login_old = await client.post(
        "/api/auth/login",
        json={"username": "ivy", "password": "Ivypassword1!"},
    )
    assert login_old.status_code == 401

    login_new = await client.post(
        "/api/auth/login",
        json={"username": "ivy", "password": "Ivy-new-password1!"},
    )
    assert login_new.status_code == 200


@pytest.mark.asyncio
async def test_set_password_rejects_weak_password(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A password too short for the policy is refused with a friendly error."""
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "jane",
            "email": "jane@example.com",
            "first_name": "Jane",
            "last_name": "K",
            "password": "Janepassword1!",
            "role": "regular_user",
        },
    )
    user_id = create.json()["id"]

    reset = await client.post(
        f"/api/admin/users/{user_id}/set-password",
        headers=auth_headers,
        json={"password": "x"},  # below the test settings' min length of 8
    )
    assert reset.status_code == 400


# --- set-teams ---------------------------------------------------------


@pytest.mark.asyncio
async def test_set_teams_bulk_replace(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Bulk replacement adds, removes, and is idempotent on the same set."""
    del seeded
    teams: list[int] = []
    for name in ("Alpha", "Beta", "Gamma"):
        r = await client.post(
            "/api/admin/teams",
            headers=auth_headers,
            json={"name": name, "description": ""},
        )
        assert r.status_code == 201
        teams.append(r.json()["id"])

    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "kim",
            "email": "kim@example.com",
            "first_name": "Kim",
            "last_name": "L",
            "password": "Kimpassword1!",
            "role": "regular_user",
            "team_ids": [teams[0]],
        },
    )
    user_id = create.json()["id"]

    # Replace {Alpha} with {Beta, Gamma}.
    swap = await client.post(
        f"/api/admin/users/{user_id}/set-teams",
        headers=auth_headers,
        json={"team_ids": [teams[1], teams[2]]},
    )
    assert swap.status_code == 200, swap.text
    names = sorted(t["name"] for t in swap.json()["teams"])
    assert names == ["Beta", "Gamma"]

    # Idempotent: same set in the body → no change to the membership.
    again = await client.post(
        f"/api/admin/users/{user_id}/set-teams",
        headers=auth_headers,
        json={"team_ids": [teams[1], teams[2]]},
    )
    assert again.status_code == 200
    assert sorted(t["name"] for t in again.json()["teams"]) == ["Beta", "Gamma"]

    # Empty list clears membership.
    cleared = await client.post(
        f"/api/admin/users/{user_id}/set-teams",
        headers=auth_headers,
        json={"team_ids": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["teams"] == []


# --- capability roles (ADR-0005) -------------------------------------


@pytest.mark.asyncio
async def test_role_listing_exposes_kind(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The role list discriminates base roles from capability roles."""
    del seeded
    r = await client.get("/api/admin/roles", headers=auth_headers)
    assert r.status_code == 200
    by_name = {role["name"]: role for role in r.json()}
    assert by_name["regular_user"]["kind"] == "base"
    assert by_name["library_curator"]["kind"] == "capability"
    assert by_name["materials_curator"]["kind"] == "capability"


@pytest.mark.asyncio
async def test_create_user_with_capability_roles(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Base role + capabilities assigned in one create call (atomic)."""
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "curator",
            "email": "curator@example.com",
            "first_name": "Cura",
            "last_name": "Tor",
            "password": "Curatorpass1!",
            "role": "regular_user",
            "capability_roles": ["materials_curator"],
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["role"] == "regular_user"
    assert body["capability_roles"] == ["materials_curator"]

    # Persisted: a fresh GET shows the capability.
    fetched = await client.get(f"/api/admin/users/{body['id']}", headers=auth_headers)
    assert fetched.json()["capability_roles"] == ["materials_curator"]


@pytest.mark.asyncio
async def test_create_user_rejects_base_role_as_capability(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A base-role name in capability_roles 400s and rolls the create back."""
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "ghost",
            "email": "ghost@example.com",
            "first_name": "Gh",
            "last_name": "Ost",
            "password": "Ghostlypass1!",
            "role": "regular_user",
            "capability_roles": ["administrator"],
        },
    )
    assert create.status_code == 400, create.text

    # Atomic: the user was not created.
    listing = await client.get("/api/admin/users", headers=auth_headers)
    assert "ghost" not in [u["username"] for u in listing.json()]


@pytest.mark.asyncio
async def test_set_capability_roles_bulk_replace(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Bulk replacement grants, revokes, and clears capability roles."""
    del seeded
    create = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "stan",
            "email": "stan@example.com",
            "first_name": "Stan",
            "last_name": "Ley",
            "password": "Stanleypass1!",
            "role": "regular_user",
        },
    )
    assert create.status_code == 201, create.text
    user_id = create.json()["id"]
    assert create.json()["capability_roles"] == []

    # Grant {library_curator}.
    granted = await client.post(
        f"/api/admin/users/{user_id}/set-capability-roles",
        headers=auth_headers,
        json={"role_names": ["library_curator"]},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["capability_roles"] == ["library_curator"]

    # Swap to {materials_curator}.
    swapped = await client.post(
        f"/api/admin/users/{user_id}/set-capability-roles",
        headers=auth_headers,
        json={"role_names": ["materials_curator"]},
    )
    assert swapped.json()["capability_roles"] == ["materials_curator"]

    # Clear.
    cleared = await client.post(
        f"/api/admin/users/{user_id}/set-capability-roles",
        headers=auth_headers,
        json={"role_names": []},
    )
    assert cleared.json()["capability_roles"] == []


# --- new-tool drafts -------------------------------------------------


@pytest.mark.asyncio
async def test_tool_drafts_require_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/tool-drafts")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tool_draft_create_update_resume_delete(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Full draft lifecycle: empty → create → update-in-place → get → delete."""
    del seeded

    # Starts empty.
    listing = await client.get("/api/admin/tool-drafts", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["drafts"] == []

    # Create.
    payload = {"alias": "my_tool", "name": "My Tool", "wizard": {"inputs": []}}
    create = await client.post(
        "/api/admin/tool-drafts",
        headers=auth_headers,
        json={"draft_id": None, "title": "My Tool", "alias": "my_tool", "payload": payload},
    )
    assert create.status_code == 200, create.text
    draft_id = create.json()["id"]
    assert create.json()["title"] == "My Tool"
    assert create.json()["payload"] == payload

    # Listed once.
    after_create = await client.get("/api/admin/tool-drafts", headers=auth_headers)
    drafts = after_create.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["id"] == draft_id

    # Update in place — same id, list stays length 1.
    new_payload = {"alias": "my_tool", "name": "My Tool v2", "wizard": {"inputs": [1]}}
    update = await client.post(
        "/api/admin/tool-drafts",
        headers=auth_headers,
        json={
            "draft_id": draft_id,
            "title": "My Tool v2",
            "alias": "my_tool",
            "payload": new_payload,
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["id"] == draft_id
    assert update.json()["title"] == "My Tool v2"
    after_update = await client.get("/api/admin/tool-drafts", headers=auth_headers)
    assert len(after_update.json()["drafts"]) == 1

    # Resume fetch returns the updated payload.
    got = await client.get(f"/api/admin/tool-drafts/{draft_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["payload"] == new_payload

    # Delete.
    deleted = await client.delete(f"/api/admin/tool-drafts/{draft_id}", headers=auth_headers)
    assert deleted.status_code == 204
    assert (await client.get("/api/admin/tool-drafts", headers=auth_headers)).json()["drafts"] == []

    # Gone → 404.
    missing = await client.get(f"/api/admin/tool-drafts/{draft_id}", headers=auth_headers)
    assert missing.status_code == 404
