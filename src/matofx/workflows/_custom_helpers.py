"""Internal helpers for the custom-workflow service paths.

Three concerns:

- **Alias synthesis.** Custom workflows live in the same ``workflow``
  table as admin-released ones, which means they share the global
  ``alias`` unique constraint. Picking the alias from the user-supplied
  display name would let two users collide ("my workflow" vs "my
  workflow"). We instead synthesise an opaque ``custom_<owner>_<slug>``
  alias that's globally unique by construction and bump a numeric
  suffix on the rare collision.
- **Tool visibility for a recipient.** When a custom-workflow owner
  adds a share recipient, every member tool must be visible to that
  recipient under the regular tool-access ladder (state gate + team
  gate). This module ships a one-call helper that wraps the
  ``ToolService`` primitives.
- **Descriptor loading.** :func:`compute_order` is pure — it asks for
  a ``{alias: descriptor}`` mapping. This module ships the SQL fetch
  that produces that mapping from a list of tool aliases.

Nothing here is part of the public API — all consumers should be
inside :mod:`matofx.workflows.service`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.tools.orm import Tool
from matofx.workflows.orm import Workflow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_MAX_BASE_SLUG_LEN = 32
"""Cap the user-derived slug so ``custom_<owner>_<slug>_<suffix>``
stays comfortably under the column's 64-char limit."""


def slugify(name: str) -> str:
    """Convert a free-text workflow name into a snake-case slug.

    Lower-cases, collapses every run of non-``[a-z0-9]`` into a single
    underscore, trims leading / trailing underscores, and caps the
    length. Empty input (or pure-symbol input that collapses to an
    empty string) yields ``"workflow"`` as a safe fallback — the
    final alias is unique either way thanks to the owner prefix.
    """
    lowered = (name or "").lower()
    slug = _SLUG_PATTERN.sub("_", lowered).strip("_")
    if not slug:
        slug = "workflow"
    return slug[:_MAX_BASE_SLUG_LEN].strip("_") or "workflow"


async def synthesise_custom_alias(
    session: AsyncSession,
    *,
    name: str,
    owner_id: int,
) -> str:
    """Pick a globally-unique alias for a new custom workflow.

    Format: ``custom_<owner_id>_<slug>`` with a ``_2``, ``_3`` … suffix
    appended on collision. The owner prefix keeps collisions extremely
    rare in practice (only when the same user reuses the same name
    twice, e.g. after archiving and re-creating).
    """
    base_slug = slugify(name)
    base = f"custom_{owner_id}_{base_slug}"
    candidate = base
    suffix = 2
    while await _alias_exists(session, candidate):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


async def _alias_exists(session: AsyncSession, alias: str) -> bool:
    """Return ``True`` when the global ``workflow.alias`` already exists."""
    stmt = select(Workflow.id).where(Workflow.alias == alias).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def user_can_see_tool(
    session: AsyncSession,
    *,
    user_id: int,
    tool: Tool,
) -> bool:
    """One-call visibility check — delegates to the canonical predicate.

    Thin wrapper over :func:`matofx.tools.service.can_user_see_tool`
    (archived + state + team gates), kept for the keyword-style call
    sites in the workflow layer (share-recipient validation, the graph
    member check). A recipient who can't see one of a workflow's member
    tools can't be added to its share list.
    """
    # Lazy import to keep the tools.service / workflows boundary one-way.
    from matofx.tools.service import can_user_see_tool  # noqa: PLC0415

    return await can_user_see_tool(session, user_id, tool)


async def load_tool_descriptors(
    session: AsyncSession,
    tool_aliases: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{alias: descriptor}`` for the supplied tool aliases.

    The result feeds :func:`matofx.workflows.ordering.compute_order`.
    Aliases the DB doesn't know about are simply omitted — the
    ordering algorithm treats those as having no declared dependencies.
    """
    if not tool_aliases:
        return {}
    stmt = select(Tool.alias, Tool.descriptor).where(Tool.alias.in_(list(set(tool_aliases))))
    rows = (await session.execute(stmt)).all()
    return {alias: descriptor if isinstance(descriptor, dict) else {} for alias, descriptor in rows}
