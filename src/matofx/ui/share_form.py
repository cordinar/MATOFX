"""Shared "share with people" picker (ADR-0002).

A compact, repeatable people+role picker reused wherever a project is
created so it can be shared in the same step: the dashboard *New
Project* and both Home Quick-Access *Launch in new project* flows. The
project page's full Share dialog (live add/change/remove) is separate
and richer; this is the create-time "grant these people now" form.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui.api_client import ApiClient, ApiError

_ROLES = ["owner", "member", "viewer"]


def render(
    directory_list: list[dict[str, Any]], creator_user_id: int | None = None
) -> dict[str, Any]:
    """Render an "add person" list; return state for :func:`collect`.

    ``creator_user_id`` (when known) is excluded from the picker — the
    Creator always has full control and can't be an ACL row.
    """
    options = {
        int(d["user_id"]): f"{d['full_name']} ({d['username']})"
        for d in directory_list
        if creator_user_id is None or int(d["user_id"]) != creator_user_id
    }
    rows: list[dict[str, Any]] = []
    container = ui.column().classes("w-full gap-1")

    def _add_row() -> None:
        with container, ui.row().classes("items-center gap-2 w-full") as row:
            person = (
                ui.select(options, label="Person", with_input=True).props("dense").classes("grow")
            )
            role = ui.select(_ROLES, value="viewer").props("dense")
            entry: dict[str, Any] = {"person": person, "role": role, "row": row}

            def _remove() -> None:
                row.delete()
                if entry in rows:
                    rows.remove(entry)

            ui.button(icon="delete", on_click=_remove).props("flat dense color=negative")
        rows.append(entry)

    ui.button("Add person", on_click=_add_row).props("flat dense icon=person_add")
    return {"rows": rows}


def collect(state: dict[str, Any]) -> list[tuple[int, str]]:
    """Return ``[(user_id, role)]`` for every filled-in picker row.

    Last-wins on a duplicated person so the apply loop can't 409 on
    itself.
    """
    grants: dict[int, str] = {}
    for e in state["rows"]:
        val = e["person"].value
        if val in (None, ""):
            continue
        grants[int(val)] = str(e["role"].value)
    return list(grants.items())


async def apply(client: ApiClient, project_id: int, grants: list[tuple[int, str]]) -> None:
    """Upsert each grant onto the project.

    Raises :class:`ApiError` on the first failure (the caller surfaces
    it; the project already exists by then).
    """
    for user_id, role in grants:
        await client.upsert_project_acl(project_id, user_id=user_id, role=role)


__all__ = ["ApiError", "apply", "collect", "render"]
