"""Per-user Quick Access favourites router.

Three routes scoped to the authenticated caller (no
``user_id`` in the path — the bearer token decides identity):

* ``GET    /api/me/favourites`` — current snapshot + feature flag.
* ``PUT    /api/me/favourites/{kind}/{alias}`` — idempotent add.
* ``DELETE /api/me/favourites/{kind}/{alias}`` — idempotent remove.

When an administrator has turned the feature off in General
Settings, reads still succeed (the response carries
``enabled=False`` so the UI hides the stars) but mutations 409 with
a plain-English message.

No permissions row is required — favouriting is a per-user UX
action, not a feature gate. The admin toggle is the only access
boundary; it lives in :mod:`matofx.config.general_settings`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import get_current_user, get_db, get_favourites_service
from matofx.api.schemas import FavouritesSnapshot
from matofx.favourites import FavouritesService
from matofx.users.orm import User

router = APIRouter()


def _snapshot_response(snapshot: object) -> FavouritesSnapshot:
    """Coerce the service dataclass into the Pydantic wire model."""
    return FavouritesSnapshot(
        enabled=snapshot.enabled,  # type: ignore[attr-defined]
        tools=list(snapshot.tools),  # type: ignore[attr-defined]
        workflows=list(snapshot.workflows),  # type: ignore[attr-defined]
    )


@router.get("/me/favourites", response_model=FavouritesSnapshot)
async def get_my_favourites(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[FavouritesService, Depends(get_favourites_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FavouritesSnapshot:
    """Return the caller's favourites + the site-wide enabled flag.

    Always succeeds for an authenticated user: when the feature is
    disabled the response carries ``enabled=False`` plus whatever
    rows the user previously saved (rather than 409-ing the UI).
    """
    snap = await service.snapshot(db, user_id=current_user.id)
    return _snapshot_response(snap)


@router.put("/me/favourites/{kind}/{alias}", response_model=FavouritesSnapshot)
async def add_my_favourite(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[FavouritesService, Depends(get_favourites_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    kind: Annotated[str, Path()],
    alias: Annotated[str, Path()],
) -> FavouritesSnapshot:
    """Star ``alias`` of ``kind`` for the caller. Idempotent.

    Returns the updated snapshot so the UI can reconcile its
    optimistic state with the server in one round trip.
    """
    snap = await service.add(db, user_id=current_user.id, kind=kind, alias=alias)
    return _snapshot_response(snap)


@router.delete("/me/favourites/{kind}/{alias}", response_model=FavouritesSnapshot)
async def remove_my_favourite(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[FavouritesService, Depends(get_favourites_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    kind: Annotated[str, Path()],
    alias: Annotated[str, Path()],
) -> FavouritesSnapshot:
    """Un-star ``alias`` of ``kind`` for the caller. Idempotent.

    Returns the updated snapshot.
    """
    snap = await service.remove(db, user_id=current_user.id, kind=kind, alias=alias)
    return _snapshot_response(snap)
