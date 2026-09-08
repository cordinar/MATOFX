"""Unit tests for :class:`FavouritesService`.

Covers the service in isolation: the admin-toggle gate on
mutations, the kind validation, the idempotent semantics, and the
alphabetical sort the wire shape relies on.

API-level tests live in
``tests/api/test_favourites_router.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.favourites import (
    FavouritesDisabledError,
    FavouritesService,
    FavouritesUnknownKindError,
)
from matofx.tools.exec_orm import RuntimeSetting


async def _set_favourites_enabled(factory: SessionFactory, value: bool) -> None:
    """Write the runtime setting row directly, bypassing the admin route.

    The model fills ``updated_at`` from its ``_utcnow`` default, so
    we only need to set the domain / key / payload here.
    """
    async with session_scope(factory) as session:
        row = await session.get(RuntimeSetting, ("general", "favourites_enabled"))
        if row is None:
            session.add(
                RuntimeSetting(
                    domain="general",
                    key="favourites_enabled",
                    value_json=json.dumps(value),
                )
            )
        else:
            row.value_json = json.dumps(value)


@pytest.mark.asyncio
async def test_snapshot_default_is_disabled_empty(
    api_factory: SessionFactory, seeded: dict[str, Any]
) -> None:
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        snap = await service.snapshot(session, user_id=seeded["user_id"])
    assert snap.enabled is False
    assert snap.tools == []
    assert snap.workflows == []


@pytest.mark.asyncio
async def test_add_when_disabled_raises(
    api_factory: SessionFactory, seeded: dict[str, Any]
) -> None:
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        with pytest.raises(FavouritesDisabledError):
            await service.add(session, user_id=seeded["user_id"], kind="tool", alias="alpha")


@pytest.mark.asyncio
async def test_add_then_remove_round_trip(
    api_factory: SessionFactory, seeded: dict[str, Any]
) -> None:
    await _set_favourites_enabled(api_factory, True)
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        snap = await service.add(session, user_id=seeded["user_id"], kind="tool", alias="alpha")
        assert snap.tools == ["alpha"]
        snap = await service.remove(session, user_id=seeded["user_id"], kind="tool", alias="alpha")
        assert snap.tools == []


@pytest.mark.asyncio
async def test_add_is_idempotent(api_factory: SessionFactory, seeded: dict[str, Any]) -> None:
    await _set_favourites_enabled(api_factory, True)
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        for _ in range(3):
            snap = await service.add(session, user_id=seeded["user_id"], kind="tool", alias="alpha")
        assert snap.tools == ["alpha"]


@pytest.mark.asyncio
async def test_remove_missing_is_noop(api_factory: SessionFactory, seeded: dict[str, Any]) -> None:
    await _set_favourites_enabled(api_factory, True)
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        snap = await service.remove(
            session, user_id=seeded["user_id"], kind="tool", alias="never_added"
        )
    assert snap.tools == []


@pytest.mark.asyncio
async def test_unknown_kind_rejected(api_factory: SessionFactory, seeded: dict[str, Any]) -> None:
    await _set_favourites_enabled(api_factory, True)
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        with pytest.raises(FavouritesUnknownKindError):
            await service.add(session, user_id=seeded["user_id"], kind="scenario", alias="x")


@pytest.mark.asyncio
async def test_snapshot_returns_alphabetical_lists(
    api_factory: SessionFactory, seeded: dict[str, Any]
) -> None:
    await _set_favourites_enabled(api_factory, True)
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        for kind, alias in (
            ("tool", "zeta"),
            ("workflow", "yankee"),
            ("tool", "alpha"),
            ("workflow", "xray"),
        ):
            await service.add(session, user_id=seeded["user_id"], kind=kind, alias=alias)
        snap = await service.snapshot(session, user_id=seeded["user_id"])
    assert snap.tools == ["alpha", "zeta"]
    assert snap.workflows == ["xray", "yankee"]


@pytest.mark.asyncio
async def test_snapshot_still_returns_rows_when_disabled(
    api_factory: SessionFactory, seeded: dict[str, Any]
) -> None:
    """Disabling the feature hides stars in the UI; it does NOT wipe rows."""
    await _set_favourites_enabled(api_factory, True)
    service = FavouritesService()
    async with session_scope(api_factory) as session:
        await service.add(session, user_id=seeded["user_id"], kind="tool", alias="alpha")
    # Flip the toggle back off.
    await _set_favourites_enabled(api_factory, False)
    async with session_scope(api_factory) as session:
        snap = await service.snapshot(session, user_id=seeded["user_id"])
    assert snap.enabled is False
    assert snap.tools == ["alpha"]
